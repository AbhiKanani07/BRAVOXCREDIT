"""Dev convenience: `python run.py` (equivalent to uvicorn ... --reload)."""
import uvicorn

if __name__ == "__main__":
    uvicorn.run("creditvox.api:app", host="127.0.0.1", port=8000, reload=True)
