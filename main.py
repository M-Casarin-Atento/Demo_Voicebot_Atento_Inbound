# -*- coding: utf-8 -*-
"""
Bridge Twilio <-> Deepgram (Media Streams) vía WebSocket.

Requisitos:
- TwiML con <Stream url="wss://TU-NGROK.ngrok-free.dev/twilio"/>
- ngrok/Proxy exponiendo https -> ws://0.0.0.0:5000
- core/config.json con la configuración del agente Deepgram
- core.settings.get_setting() que exponga API_KEY_DEEPGRAM

Nota: Este servidor NO sirve HTTP; solo el WS de Twilio.
"""

import asyncio
import base64
import json
from pathlib import Path

import websockets
from websockets.exceptions import ConnectionClosedOK, ConnectionClosedError

from core.settings import get_setting

# ---------------------------------------------------------------------
# Configuración
# ---------------------------------------------------------------------

settings = get_setting()
CORE_PATH = Path("core")
BUFFER_SIZE = 20 * 160  # ~20ms @ 8kHz μ-law (160 bytes por 20ms)

# ---------------------------------------------------------------------
# Utilidades
# ---------------------------------------------------------------------

def load_config() -> dict:
    """
    Cargar el archivo de configuración del agente (core/config.json).
    """
    file_path = CORE_PATH / "config.json"
    if not file_path.exists():
        raise FileNotFoundError("Archivo config.json no encontrado en la carpeta core")
    with open(file_path, "r", encoding="utf-8") as f:
        return json.load(f)


def sts_connect():
    """
    Conexión al WS de Deepgram.
    Recomendado: token por header Authorization (más seguro que subprotocols).
    """
    api_key = getattr(settings, "API_KEY_DEEPGRAM", None)
    if not api_key:
        raise RuntimeError("Apikey de Deepgram no ha sido encontrada (API_KEY_DEEPGRAM).")

    return websockets.connect(
        "wss://agent.deepgram.com/v1/agent/converse",
        extra_headers=[("Authorization", f"Token {api_key}")],
        ping_interval=20,
        ping_timeout=20,
        max_size=2**22,  # 4 MiB
    )

# ---------------------------------------------------------------------
# Handlers Deepgram -> Twilio
# ---------------------------------------------------------------------

async def handle_barge_in(decoded: dict, twilio_ws, streamsid: str) -> None:
    """
    Manejo de barge-in básico: si el usuario empieza a hablar, limpiar cola de salida.
    """
    if decoded.get("type") == "UserStartedSpeaking":
        clear_message = {"event": "clear", "streamSid": streamsid}
        await twilio_ws.send(json.dumps(clear_message))


async def handle_text_message(decoded: dict, twilio_ws, sts_ws, streamsid: str) -> None:
    """
    Procesa mensajes JSON provenientes de Deepgram (eventos, texto, etc.).
    """
    await handle_barge_in(decoded, twilio_ws, streamsid)
    # TODO: function calling / comandos / control adicional


async def sts_sender(sts_ws, audio_queue: asyncio.Queue) -> None:
    """
    Envía chunks de audio (bytes μ-law) hacia Deepgram.
    Usa sentinel None para finalizar.
    """
    print("[sts_sender] iniciado")
    try:
        while True:
            chunk = await audio_queue.get()
            if chunk is None:
                break
            await sts_ws.send(chunk)
    except (ConnectionClosedOK, ConnectionClosedError):
        pass
    finally:
        print("[sts_sender] finalizado")


async def sts_receiver(sts_ws, twilio_ws, streamsid_queue: asyncio.Queue) -> None:
    """
    Recibe de Deepgram: si es texto, procesa; si son bytes, los reenvía a Twilio como outbound.
    """
    print("[sts_receiver] iniciado")
    streamsid = await streamsid_queue.get()

    try:
        async for message in sts_ws:
            # Deepgram puede enviar texto (JSON) o audio (bytes)
            if isinstance(message, str):
                print(f"[Deepgram JSON] {message}")
                try:
                    decoded = json.loads(message)
                except json.JSONDecodeError:
                    continue
                await handle_text_message(decoded, twilio_ws=twilio_ws, sts_ws=sts_ws, streamsid=streamsid)
                continue

            # Audio binario (μ-law 8kHz) -> Twilio outbound
            raw_mulaw = message
            media_message = {
                "event": "media",
                "streamSid": streamsid,
                "media": {
                    "track": "outbound",
                    "payload": base64.b64encode(raw_mulaw).decode("ascii"),
                },
            }
            await twilio_ws.send(json.dumps(media_message))

    except (ConnectionClosedOK, ConnectionClosedError):
        pass
    finally:
        print("[sts_receiver] finalizado")

# ---------------------------------------------------------------------
# Handler Twilio -> Deepgram
# ---------------------------------------------------------------------

async def twilio_receiver(twilio_ws, audio_queue: asyncio.Queue, streamsid_queue: asyncio.Queue) -> None:
    """
    Recibe eventos de Twilio (start, media, stop).
    Acumula audio inbound y lo envía a Deepgram en bloques BUFFER_SIZE.
    """
    print("[twilio_receiver] iniciado")
    inbuffer = bytearray()

    try:
        async for message in twilio_ws:
            try:
                data = json.loads(message) if isinstance(message, str) else None
            except json.JSONDecodeError:
                continue

            if not data:
                continue

            event = data.get("event")

            if event == "connected":
                # Handshake inicial
                continue

            elif event == "start":
                print("[twilio_receiver] start → obteniendo streamSid")
                start = data.get("start", {})
                streamsid = start.get("streamSid")
                if streamsid:
                    streamsid_queue.put_nowait(streamsid)

            elif event == "media":
                media = data.get("media", {})
                payload = media.get("payload")
                track = media.get("track", "inbound")
                if not payload or track != "inbound":
                    continue

                # Twilio envía μ-law 8kHz base64
                try:
                    chunk = base64.b64decode(payload)
                except Exception:
                    continue

                inbuffer.extend(chunk)

                # Cortar en bloques regulares para Deepgram
                while len(inbuffer) >= BUFFER_SIZE:
                    out_chunk = bytes(inbuffer[:BUFFER_SIZE])
                    await audio_queue.put(out_chunk)
                    del inbuffer[:BUFFER_SIZE]

            elif event == "stop":
                print("[twilio_receiver] stop")
                break

            # Ignorar silenciosamente otros eventos si aparecen
    except (ConnectionClosedOK, ConnectionClosedError):
        pass
    finally:
        # Flush remanente y avisar cierre a Deepgram
        if inbuffer:
            await audio_queue.put(bytes(inbuffer))
        await audio_queue.put(None)
        print("[twilio_receiver] finalizado")

# ---------------------------------------------------------------------
# Orquestación por conexión WS de Twilio
# ---------------------------------------------------------------------

async def twilio_handler(twilio_ws, path):
    """
    Ruta WS para Twilio Media Streams.
    Debe coincidir con el path configurado en el TwiML (<Stream url=".../twilio" />).
    """
    if path != "/twilio":
        await twilio_ws.close()
        return

    audio_queue: asyncio.Queue = asyncio.Queue(maxsize=100)
    streamsid_queue: asyncio.Queue = asyncio.Queue()

    try:
        async with sts_connect() as sts_ws:
            # Enviar configuración inicial del agente Deepgram
            config_message = load_config()
            await sts_ws.send(json.dumps(config_message))

            # Correr tareas en paralelo
            sender_task = asyncio.create_task(sts_sender(sts_ws, audio_queue))
            receiver_task = asyncio.create_task(sts_receiver(sts_ws, twilio_ws, streamsid_queue))
            twilio_task = asyncio.create_task(twilio_receiver(twilio_ws, audio_queue, streamsid_queue))

            done, pending = await asyncio.wait(
                {sender_task, receiver_task, twilio_task},
                return_when=asyncio.FIRST_EXCEPTION,
            )

            # Propagar excepciones si alguna falló
            for task in done:
                exc = task.exception()
                if exc:
                    raise exc

    finally:
        await twilio_ws.close()

# ---------------------------------------------------------------------
# Servidor
# ---------------------------------------------------------------------

async def main():
    async with websockets.serve(
        twilio_handler,
        host="0.0.0.0",
        port=5000,
        max_size=2**22,
    ):
        print("Servidor iniciado en ws://0.0.0.0:5000/twilio")
        try:
            await asyncio.Future()  # ejecutar hasta señal/cancelación
        except asyncio.CancelledError:
            pass


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except Exception as e:
        print(f"[Error] {e}")
