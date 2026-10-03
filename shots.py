
import asyncio, os, subprocess, time, signal, sys
from playwright.async_api import async_playwright
PORT = 5077
BASE = "http://127.0.0.1:%d" % PORT
OUT = "/workspace/fleetfoot/preview"
os.makedirs(OUT, exist_ok=True)
env = dict(os.environ, DB_PATH="/workspace/fleetfoot/demo.db", PORT=str(PORT), TZ="America/Chicago")
srv = subprocess.Popen([sys.executable, "app.py"], cwd="/workspace/fleetfoot", env=env,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
time.sleep(3)

async def main():
    async with async_playwright() as p:
        br = await p.chromium.launch(executable_path='/workspace/.cache/ms-playwright/chromium-1243/chrome-linux64/chrome', args=['--no-sandbox','--disable-dev-shm-usage'], )
        async def shot(path, name, setup=None, w=1440, h=980, wait=2200):
            ctx = await br.new_context(viewport={"width":w,"height":h}, device_scale_factor=2)
            pg = await ctx.new_page()
            if setup: await setup(pg)
            await pg.goto(BASE+path); await pg.wait_for_timeout(wait)
            await pg.screenshot(path=os.path.join(OUT,name), full_page=False)
            await ctx.close(); print("shot", name)

        async def dispatch_login(pg):
            await pg.goto(BASE+"/dispatch/login")
            await pg.fill("input[name=username]","admin"); await pg.fill("input[name=password]","dispatch123")
            await pg.click("form button"); await pg.wait_for_timeout(700)
        async def driver_login(pg):
            await pg.goto(BASE+"/driver/login")
            await pg.fill("input[name=phone]","3345550111"); await pg.fill("input[name=pin]","1234")
            await pg.click("form button"); await pg.wait_for_timeout(700)
        async def rest_login(pg):
            await pg.goto(BASE+"/restaurant/login")
            await pg.fill("input[name=slug]","tigertown"); await pg.fill("input[name=pin]","1111")
            await pg.click("form button"); await pg.wait_for_timeout(700)

        # customer: menu with a couple of items in the cart
        async def cust(pg):
            await pg.goto(BASE+"/r/1"); await pg.wait_for_timeout(1200)
            for sel in await pg.query_selector_all("button"):
                t = (await sel.inner_text()).strip().lower()
                if t.startswith("add"):
                    await sel.click(); await pg.wait_for_timeout(300)
                    break
        await shot("/r/1","1_customer_order.png", cust, h=1100)
        await shot("/dispatch","2_dispatch_board.png", dispatch_login, h=1150)
        await shot("/dispatch/manage","3_dispatch_manage.png", dispatch_login, h=1150)
        await shot("/driver","4_driver_app.png", driver_login, w=520, h=980)
        await shot("/restaurant","5_restaurant_app.png", rest_login, w=1100, h=900)
        await shot("/dispatch/account","6_dispatchers.png", dispatch_login, h=980)
        await shot("/dispatch/map","7_driver_map.png", dispatch_login, h=980, wait=4000)
        await shot("/dispatch/schedule","8_dispatch_schedule.png", dispatch_login, h=1150)
        await shot("/dispatch/new-order?from=1","9_new_order_from.png", dispatch_login, h=1200, wait=3000)
        await shot("/driver","10_driver_week.png", driver_login, w=520, h=1250, wait=3000)
        await br.close()

asyncio.run(main())
srv.send_signal(signal.SIGTERM)
