"""Bind the process to the platform port. Render sets PORT."""

import os

import uvicorn


def main():
    port = int(os.environ.get("PORT", "8000"))
    uvicorn.run(
        "aerodoppler.app:app",
        host="0.0.0.0",
        port=port,
        log_level="info",
        proxy_headers=True,
        forwarded_allow_ips="*",
        access_log=True,
    )


if __name__ == "__main__":
    main()
