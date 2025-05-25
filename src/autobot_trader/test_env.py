# src/autobot_trader/test_env.py
import os
from dotenv import load_dotenv

load_dotenv()
print("ACCESS KEY:", os.getenv("UPBIT_ACCESS_KEY"))
print("SECRET KEY:", os.getenv("UPBIT_SECRET_KEY"))
print("TELEGRAM TOKEN:", os.getenv("TELEGRAM_TOKEN"))
