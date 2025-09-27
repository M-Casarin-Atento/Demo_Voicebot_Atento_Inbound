import base64
import websockets
import os 
from core.settings import get_setting
from pathlib import Path

settings = get_setting()

def sts_connect():
    """
    Permite la conexion al websocket y nos permite iniciar la comunicacion con Deepgram
    """
    API_KEY = settings.API_KEY_DEEPGRAM
    if not API_KEY: 
        raise Exception(f"Apikey de Deepgram no ha sido encontrada")
    sts_ws = websockets.connect(
        "wss://agent.deepgram.com/v1/agent/converse",
        subprotocols=["token", API_KEY]
    )
    return sts_ws


import json 
CORE_PATH = Path("core")
def load_config():
    """
    Cargar el archivo de configuracion
    """
    file_path = CORE_PATH / "config.json"
    if not file_path.exists():
        raise FileNotFoundError("Archivo config.json no encontrado en la carpeta core")
    
    with open(file_path, "r") as f: 
        return json.load(f)
    


# ------- Funciones Websocket con twilio -------
# sts es del agente de voz 

async def handle_barge_in(decoded, twilio_ws, streamsid):
    if decoded["type"] == "UserStartedSpeaking": 
        clear_message = {
            "event": "clear", 
            "streamSid": streamsid
        }

        await twilio_ws.send(json.dumps(clear_message))


async def handle_text_message(decoded, twilio_ws, sts_ws, streamsid):
    await handle_barge_in(decoded, twilio_ws, streamsid)
    # TODO: handle function calling 



async def sts_sender(sts_ws, audio_queue):
    print("Sts_sender inciado")
    while True: 
        chunk = await audio_queue.get()
        await sts_ws.send(chunk)



async def sts_receiver(sts_ws, twilio_ws, streamsid_queue):
    print("sts_receiver inciado")
    streamsid = await streamsid_queue.get()

    async for message in sts_ws: 
        if isinstance(message, str):
            print(message)
            decoded = json.loas(message)
            await handle_text_message(decoded, twilio_ws=twilio_ws, sts_ws=sts_ws, streamsid=streamsid)
            continue 

        raw_mulaw = message

        media_message = {
            "event": "media", 
            "streamSid": streamsid, 
            "media": {
                "payload": base64.b64encode(raw_mulaw).decode("ascii")
            }
        }

        await twilio_ws.send(json.dumps(media_message))


async def twilio_receiver(twilio_ws, audio_queue, streamsid_queue):
    # BUFFER SIZE es cuanto de audio esencialmente queremos almacenar antes antes de iniciar a mandar el mensaje de salida
    BUFFER_SIZE = 20 * 160 # Lo reciimos y hacemos particiones al audio 
    inbuffer = bytearray(b"")
    
    async for message in twilio_ws: 
        try:
            data = json.load(message)
            event = data['event']

            if event == "start":
                print(f"Obtener el streamsid")
                start = data['start']
                streamsid = start['streamSid']
                streamsid_queue.put_nowait(streamsid)
                
            elif event == "connected":
                continue 

            elif event == "media": 
                media = data['media']
                chunk = base64.b64decode(media['payload'])

                if media['track'] == 'inbound': 
                    inbuffer.extend(chunk)

            elif event == "stop": 
                break
            
            while len(inbuffer) == BUFFER_SIZE: 
                chunk = inbuffer[: BUFFER_SIZE]
                audio_queue.put_nowat(chunk)
                inbuffer = inbuffer[BUFFER_SIZE:]

        except Exception as e: 
            break



async def twilio_handler(twilio_ws):
    audio_queue = asyncio.Queue()
    streamsid_queue = asyncio.Queue() # strea, actual que tenemos de twilio, porque podemos tener multiples usuarios 

    async with sts_connect() as sts_ws: 
        config_message = load_config()
        await sts_ws.send(json.dump(config_message))

        await asyncio.wait(
            [
                asyncio.ensure_future(sts_sender(sts_ws, audio_queue=audio_queue)),
                asyncio.ensure_future(sts_receiver(sts_ws=sts_ws, twilio_ws=twilio_ws, streamsid_queue=streamsid_queue)), 
                asyncio.ensure_future(twilio_receiver(twilio_ws=twilio_ws, audio_queue=audio_queue, streamsid_queue=streamsid_queue))
            ]
        )

        await twilio_ws.close()




# ------- Funciones varias -------

import asyncio
async def main():
    await websockets.serve(twilio_handler, host="localhost", port=5000)
    print("Servidor iniciado.")
    await asyncio.Future() # correr esto asta que salgamos de la aplicacion 

if __name__ == "__main__":
    try: 
        asyncio.run(main())
    except Exception as e:
        print(f"[Error] {e}")