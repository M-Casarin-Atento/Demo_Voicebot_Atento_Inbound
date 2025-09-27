"""
Modulo para cargar las variables del entorno
"""

from pydantic import BaseModel
from dotenv import load_dotenv
import os 
from functools import lru_cache

load_dotenv()

class Settings(BaseModel):
    VERSION: str = os.getenv("VERSION")
    API_KEY_DEEPGRAM: str = os.getenv("API_KEY_DEEPGRAM")

@lru_cache 
def get_setting() -> Settings: 
    return Settings()

