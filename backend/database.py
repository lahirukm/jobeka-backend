import os
from motor.motor_asyncio import AsyncIOMotorClient
from dotenv import load_dotenv

load_dotenv()

MONGO_URI = os.getenv("MONGO_URI")
DB_NAME   = os.getenv("DB_NAME", "jobeka")

client = AsyncIOMotorClient(MONGO_URI)
database = client[DB_NAME]

# Collections
jobs_collection = database.get_collection("jobs")


async def ping_db():
    """Test the MongoDB connection. Logs the result but won't crash the server."""
    try:
        await client.admin.command("ping")
        print(f"✅ MongoDB Connected: {DB_NAME}")
    except Exception as e:
        print(f"❌ MongoDB Error: {e}")
        print("⚠️  Server will still start, but database calls will fail until this is fixed.")
