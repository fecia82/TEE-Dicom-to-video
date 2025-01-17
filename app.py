import eventlet
eventlet.monkey_patch()

import os
import zipfile
import tempfile
import uuid
import shutil
import pydicom
import numpy as np
import cv2
from PIL import Image
from flask import Flask, request, send_file, render_template
from flask_socketio import SocketIO, emit, join_room

app = Flask(__name__)
app.config['SECRET_KEY'] = 'secret!'
app.config['MAX_CONTENT_LENGTH'] = 100 * 1024 * 1024  # 100 MB

socketio = SocketIO(app, async_mode='eventlet')

def crear_directorio_temp():
    return tempfile.mkdtemp()

def descomprimir_zip(ruta_zip, carpeta_destino):
    with zipfile.ZipFile(ruta_zip, 'r') as zip_ref:
        zip_ref.extractall(carpeta_destino)

def comprimir_carpeta(ruta_carpeta, ruta_zip_salida):
    with zipfile.ZipFile(ruta_zip_salida, 'w', zipfile.ZIP_DEFLATED) as zipf:
        for carpeta_raiz, _, archivos in os.walk(ruta_carpeta):
            for archivo in archivos:
                ruta_archivo = os.path.join(carpeta_raiz, archivo)
                arcname = os.path.relpath(ruta_archivo, start=ruta_carpeta)
                zipf.write(ruta_archivo, arcname)

def procesar_dicom(ruta_archivo, carpeta_salida):
    try:
        ds = pydicom.dcmread(ruta_archivo)
        arr = ds.pixel_array

        # Un solo frame
        if arr.ndim == 2:
            img_pil = Image.fromarray(arr)
            nombre_imagen = f"{os.path.splitext(os.path.basename(ruta_archivo))[0]}.png"
            img_pil.save(os.path.join(carpeta_salida, nombre_imagen))

        # Múltiples frames
        elif arr.ndim == 3:
            if arr.shape[-1] == 3 and arr.shape[0] != 3:
                img_pil = Image.fromarray(arr, 'RGB')
                nombre_imagen = f"{os.path.splitext(os.path.basename(ruta_archivo))[0]}.png"
                img_pil.save(os.path.join(carpeta_salida, nombre_imagen))
            else:
                frames = []
                if arr.shape[-1] == 3:
                    for frame in arr:
                        frames.append(cv2.cvtColor(frame, cv2.COLOR_RGB2BGR))
                else:
                    for frame in arr:
                        frames.append(cv2.cvtColor(frame, cv2.COLOR_GRAY2BGR))
                if frames:
                    alto, ancho, _ = frames[0].shape
                    fps = 10
                    video_salida = os.path.join(carpeta_salida, f"{os.path.splitext(os.path.basename(ruta_archivo))[0]}.avi")
                    fourcc = cv2.VideoWriter_fourcc(*"XVID")
                    out = cv2.VideoWriter(video_salida, fourcc, fps, (ancho, alto))
                    for frame in frames:
                        out.write(frame)
                    out.release()

        elif arr.ndim == 4:
            frames = []
            for frame in arr:
                frames.append(cv2.cvtColor(frame, cv2.COLOR_RGB2BGR))
            if frames:
                alto, ancho, _ = frames[0].shape
                fps = 10
                video_salida = os.path.join(carpeta_salida, f"{os.path.splitext(os.path.basename(ruta_archivo))[0]}.avi")
                fourcc = cv2.VideoWriter_fourcc(*"XVID")
                out = cv2.VideoWriter(video_salida, fourcc, fps, (ancho, alto))
                for frame in frames:
                    out.write(frame)
                out.release()
    except Exception as e:
        print(f"Error procesando {ruta_archivo}: {e}")

def procesar_zip(zip_path, output_zip_path, session_id):
    try:
        # Descomprimir el zip
        carpeta_temp = crear_directorio_temp()
        carpeta_salida = crear_directorio_temp()
        descomprimir_zip(zip_path, carpeta_temp)

        # Buscar archivos DICOM
        archivos_dicom = []
        for raiz, _, archivos in os.walk(carpeta_temp):
            for archivo in archivos:
                ruta_archivo = os.path.join(raiz, archivo)
                try:
                    pydicom.dcmread(ruta_archivo, stop_before_pixels=True)
                    archivos_dicom.append(ruta_archivo)
                except:
                    pass

        total_files = len(archivos_dicom)
        if total_files == 0:
            socketio.emit('finished', {'filename': None}, room=session_id)
            return

        # Procesar archivos DICOM
        for idx, dicom_file in enumerate(archivos_dicom, start=1):
            procesar_dicom(dicom_file, carpeta_salida)
            percent = int((idx / total_files) * 100)
            socketio.emit('progress', {'percent': percent}, room=session_id)

        # Comprimir resultados
        comprimir_carpeta(carpeta_salida, output_zip_path)

        # Generar un nombre único para el archivo de descarga
        unique_id = uuid.uuid4().hex
        final_zip_path = os.path.join(tempfile.gettempdir(), unique_id + ".zip")
        os.rename(output_zip_path, final_zip_path)

        # Emitir evento de finalización
        socketio.emit('finished', {'filename': unique_id + ".zip"}, room=session_id)

    except Exception as e:
        print(f"Error procesando el zip: {e}")
        socketio.emit('finished', {'filename': None}, room=session_id)
    finally:
        # Limpiar directorios temporales
        if os.path.exists(carpeta_temp):
            shutil.rmtree(carpeta_temp)
        if os.path.exists(carpeta_salida):
            shutil.rmtree(carpeta_salida)

@app.route("/", methods=["GET"])
def index():
    return render_template("index.html")

@app.route("/upload", methods=["POST"])
def upload():
    if "zipfile" not in request.files:
        return {"error": "No se envió ningún archivo ZIP."}, 400

    archivo_zip = request.files["zipfile"]
    if archivo_zip.filename == "":
        return {"error": "El archivo ZIP no es válido."}, 400

    # Obtener el session_id desde el encabezado
    session_id = request.headers.get('X-Session-ID')
    if not session_id:
        return {"error": "No se proporcionó un session_id."}, 400

    # Crear carpetas temporales
    carpeta_temp = crear_directorio_temp()
    carpeta_salida = crear_directorio_temp()

    try:
        # Guardar zip en temp y descomprimir
        ruta_zip = os.path.join(carpeta_temp, "subida.zip")
        archivo_zip.save(ruta_zip)

        # Crear un zip para la salida
        output_zip_path = os.path.join(carpeta_salida, "resultados.zip")

        # Iniciar el procesamiento en una tarea de fondo
        socketio.start_background_task(target=procesar_zip, zip_path=ruta_zip, output_zip_path=output_zip_path, session_id=session_id)

        return {"message": "Archivo subido y procesamiento iniciado."}, 202

    except Exception as e:
        print(f"Error: {e}")
        return {"error": "Ocurrió un error durante el procesamiento."}, 500

@app.route("/download_result", methods=["GET"])
def download_result():
    filename = request.args.get('file')
    if not filename:
        return "No se especificó ningún archivo para descargar.", 400

    ruta_zip = os.path.join(tempfile.gettempdir(), filename)
    if not os.path.exists(ruta_zip):
        return "Archivo no encontrado.", 404

    return send_file(ruta_zip, as_attachment=True, download_name="resultados.zip")

@socketio.on('start_processing')
def handle_start_processing(data):
    session_id = data.get('session_id')
    if session_id:
        join_room(session_id)
        emit('joined_room', {'message': 'Unido a la sala de procesamiento.'}, room=session_id)
    else:
        emit('error', {'message': 'No se proporcionó un session_id.'})

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    socketio.run(app, debug=True, host='0.0.0.0', port=port)
