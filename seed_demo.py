
import os, sys, sqlite3, json
sys.path.insert(0,"/workspace/tigertowntogo")
os.environ["DB_PATH"]="/workspace/tigertowntogo/demo.db"
if os.path.exists(os.environ["DB_PATH"]): os.remove(os.environ["DB_PATH"])
import app
c = app.app.test_client()
c.post("/dispatch/login", data={"username":"admin","password":"dispatch123"})
con = sqlite3.connect(os.environ["DB_PATH"]); con.row_factory=sqlite3.Row
def items(rid, n=2):
    rows = con.execute("SELECT * FROM menu_items WHERE restaurant_id=? LIMIT ?", (rid,n)).fetchall()
    return [{"menu_item_id":r["id"],"name":r["name"],"qty":1,"price_cents":r["price_cents"]} for r in rows]
def order(rid, name, phone, addr, tip=300, note=""):
    return c.post("/checkout", json={"restaurant_id":rid,"customer_name":name,"customer_phone":phone,
        "address":addr,"placed_by":"dispatch","tip_cents":tip,"note":note,"items":items(rid)}).get_json()

a = order(1,"Ruth Kelley","3345550188","2302 Waverly Parkway, Opelika, AL 36801",400,"Gate code 4412")
b = order(1,"Sam Dial","3345550177","1608 2nd Ave, Opelika, AL 36801",200)
d1 = order(2,"Tanya Rowe","3345550166","160 N College St, Auburn, AL 36830",500)
e = order(1,"Marco Pitts","3345550155","2302 Waverly Parkway, Opelika, AL 36801",0)

drivers = con.execute("SELECT * FROM drivers ORDER BY id").fetchall()
for d in drivers[:2]:
    c.post("/api/dispatch/driver-status", json={"driver_id":d["id"],"status":"online"})
c.post("/api/dispatch/driver-status", json={"driver_id":drivers[2]["id"],"status":"break"})

# two orders confirmed by the kitchen and running with a driver
c.post("/api/order/timer", json={"order_id":a["order_id"],"minutes":14})
c.post("/api/order/timer", json={"order_id":d1["order_id"],"minutes":18})
c.post("/api/dispatch/assign", json={"order_id":a["order_id"],"driver_id":drivers[0]["id"]})
c.post("/api/dispatch/assign", json={"order_id":d1["order_id"],"driver_id":drivers[0]["id"]})
# one held back into pending by dispatch
c.post("/api/order/timer", json={"order_id":b["order_id"],"minutes":10})
c.post("/api/dispatch/assign", json={"order_id":b["order_id"],"driver_id":drivers[1]["id"]})
c.post("/api/order/hold", json={"order_id":b["order_id"],"reason":"held by dispatch"})
c.post("/api/order/note", json={"order_id":a["order_id"],"note":"Gate code 4412, call on arrival."})
# chat
c.post("/api/chat/send", json={"driver_id":drivers[0]["id"],"body":"Heading to Tiger Town now."})
c.post("/api/chat/send", json={"driver_id":drivers[0]["id"],"body":"Kitchen says 5 more minutes on FF."})
# driver pings so the map has pins
c.post("/driver/login", data={"phone":"3345550111","pin":"1234"})
c.post("/api/driver/ping", json={"lat":32.6470,"lng":-85.3620})
c.post("/api/chat/send", json={"driver_id":drivers[0]["id"],"body":"On my way, 6 minutes out."})
c.post("/driver/login", data={"phone":"3345550122","pin":"1234"})
c.post("/api/driver/ping", json={"lat":32.6210,"lng":-85.4550})
print("seeded", a["code"], b["code"], d1["code"])
