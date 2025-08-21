# web.py
import os
from aiohttp import web

async def handle(req):
    return web.Response(text="OK")

app = web.Application()
app.router.add_get("/", handle)

if __name__ == "__main__":
    port = int(os.getenv("PORT", "8080"))
    web.run_app(app, port=port)
