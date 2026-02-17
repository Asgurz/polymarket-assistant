import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "src"))

import uvicorn
from web_server import app  # noqa: F401

if __name__ == "__main__":
    print("\n═══ Polymarket Crypto Assistant ═══")
    print("  Web UI: http://localhost:8080")
    print("  Press Ctrl+C to stop\n")
    uvicorn.run(
        "web_server:app",
        host="0.0.0.0",
        port=8080,
        reload=False,
        log_level="warning",
    )
