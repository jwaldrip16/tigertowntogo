# Fleet Delivery

A working four-app delivery platform: customer ordering site, dispatcher portal, driver app and
restaurant app. One Flask process, one SQLite file, no build step.

## Run it

    cd fleetdelivery
    pip install -r requirements.txt
    TZ=America/Chicago python app.py     # http://localhost:5000

Demo logins

| Surface    | URL                | Credentials                                   |
|------------|--------------------|-----------------------------------------------|
| Customer   | /                  | none                                           |
| Dispatch   | /dispatch          | admin / dispatch123                            |
| Driver     | /driver            | 3345550111 / 1234 (also ...0122, ...0133)      |
| Restaurant | /restaurant        | popeyeschicken / 1111, niffersplace / 1111 (any store: its code and PIN 1111) |

## What each surface does

**Customer site.** Browse open restaurants (open/closed comes from the live hours table), build a
cart, type an address. The address is validated and geocoded before checkout; an address that does
not resolve blocks the order. Tracking page shows kitchen timer, queue position, driver and a
navigation link.

**Dispatcher portal.** Live order board with kitchen status, dispatch status, queue number and hold
reason. Set or clear a driver's status (online, break, off), approve the status a driver requested,
assign or reassign orders, stack several on one driver, start the kitchen timer, mark ready, hold an
order, walk an order through received / at restaurant / enroute / complete, and chat with any driver.
Separate tabs for live orders and completed orders, and a completed order can be reopened either
back into the queue or straight onto the same driver. The driver panel is sorted by turn and labels
who is 1st up, 2nd up and so on, or shows at stack limit. The restaurant panel pauses and resumes any
kitchen in one click. Add restaurants and menu items under /dispatch/restaurants and
/dispatch/menu/<id>, edit hours per day under /dispatch/restaurants, and edit the fee table under
/dispatch/settings.

**Driver app.** Drivers cannot flip their own status. They tap Request online / Request break /
Request clock out, or just type it in chat ("going on break"), and it lands with dispatch as a
pending request until dispatch approves it. Their run lists only their own stops in stack order,
with navigation to the restaurant and to the customer and stage buttons for received, at restaurant,
enroute and complete. The status panel shows their own rotation position (1st up, 2nd up, or at your
stack limit) with how many orders are waiting to go out, and each stop is labelled stop 1 of 3 with
its current stage. A newly assigned order plays an audible alert that repeats every five
seconds until the driver taps Received on that stop, and vibrates the phone where the browser allows
it. Both apps have a Sound on/off button; phones need one tap
anywhere on the page before a browser will let any site play audio, which the apps prompt for. Pending unassigned orders are not shown to drivers at all. Complete pops a
warning first; after the driver confirms, the order leaves the run and lands in completed orders.

**Restaurant app.** Pending orders arrive, the store accepts with a prep timer (counts down for the
customer, dispatch and the driver), marks ready, and can pause incoming orders. One button flips the
store to open 24 hours, which overrides the weekly hours table until it is switched back; dispatch
has the same 24h button on the board and a checkbox on the hours page. A new ticket plays an audible alert that
repeats every five seconds until the store accepts it or sets a timer, with a Silence button on the
banner. The timer is adjustable at any point with -5 / +5 or by typing the minutes and pressing Set
timer, in the restaurant app and on the dispatch board.

## Delivery fee

First 3 miles $3.99, then $1.00 per additional mile, rounded up. Distance is restaurant to validated
customer address. All three numbers are editable in /dispatch/settings.

## Driver rotation

The line is fewest orders first, then whoever has waited longest since their last order. Clocking
on puts a driver at the back of the line, so a driver who just came online does not jump ahead of
drivers who have been sitting.

## Queue and holds

Orders go out to a driver as soon as they are placed, before the kitchen has accepted them, so the
driver sees the run early (set assign_on_pending to 0 in the settings table to wait for the kitchen
instead). Orders with no driver sit in one queue, oldest first, numbered for the customer and the
dispatcher.
If nobody is online, or every online driver is at their stack limit, the order is held with the
reason "no driver available" and promotes itself the moment capacity appears. Auto assign can be
turned off in settings so dispatch places every order by hand. Stack limit is per driver, default 3.

## Address validation and maps

Set GOOGLE_MAPS_API_KEY to use Google Geocoding; with no key it falls back to OpenStreetMap
Nominatim, and demo addresses are cached so it works offline. Distance is great circle times a 1.3
road factor; swap in Distance Matrix for exact road miles. Navigation links open Google Maps
directions, so they work on a phone as well as a desktop.

## Before going live

Replace the plain text PINs and passwords with hashed credentials, put it behind HTTPS, add a real
payment processor at checkout if you ever want one, and move the 4 second polling to websockets if you go past a few
dozen concurrent drivers.


## What changed in this build

- Orders sit as **pending** until the restaurant confirms. The moment the kitchen accepts (sets a timer or marks ready), the order is assigned to the next driver in line.
- A driver holding an order **drops out of the rotation** until it is completed. The dispatch board shows each driver's active run (order code, stop number, stage, restaurant to customer).
- **Drag and drop**: drag an order card onto a driver card to assign it. Dropping onto a busy driver stacks it as their next stop. Dragging within a driver's run reorders the stops (`/api/dispatch/reorder`).
- **Edit items, delivery fee and tip** on any live order from the board ("Edit items / fee / tip").
- **Dispatcher can create orders** at /dispatch/new-order, with a fee override and a tip. Dispatcher orders go through even when a kitchen is closed or the customer is blocked.
- **Block a customer** from the website by phone number, from the left column of the board. Blocked numbers get told to call dispatch.
- **Customers can tip** at checkout: 15 / 18 / 20 / 25 percent buttons or a custom dollar amount. The tip flows into the total and shows on the board.

Reminder: this build changed the database schema, so delete `delivery.db` before starting it.


## Latest build

- Unlimited stacking. A driver can carry as many stops as you drag onto them; the old
  per-driver stack limit is gone (setting `unlimited_stack`, on by default).
- Unlimited pending queue. Orders never fall out of the board when no driver is free;
  they sit in the pending/held list until a kitchen confirms and a driver frees up.
- Flashing alert. A new pending order flashes a red bar on the dispatch board and in the
  restaurant app, the order card blinks, and the alert tone loops until it is picked up.
- Order codes are now collision-proof when many orders land in the same second.
- `preview/` holds four images of the four screens.

Delete `delivery.db` before running this build; the schema changed.


## Latest build

- Dispatch board is split in two: **Pending** (waiting on the kitchen or on a free driver, nothing on a driver yet)
  and **Active runs** (already assigned, received, picked up, en route).
- **Drag and drop moves orders between drivers.** Drag a stop out of one driver's run and drop it on another
  driver and it leaves the first run, lands at the end of the new one, the old run's stop numbers close up,
  and the first driver gets a chat note that it moved. Drop a stop on the Pending column to send it back to
  the queue. A stop already picked up (en route) is refused, since the food is in that car.
- **Restaurants and drivers** page at /dispatch/manage: add and delete restaurants, add menu items,
  and build combo sub layers (e.g. "Pick your side" required pick 1, "Add ons" pick up to 3, each choice with
  its own price bump). Add drivers, delete drivers, edit names, phone numbers and passwords.
- Combo choices flow all the way through: the customer picks them on the menu, and the driver app and the
  kitchen ticket both spell them out, e.g. "2 x 3 pc Combo (Pick your side: Mac and cheese +$1.50)".
- Driver app and restaurant app now show the **full order detail**: customer name, tap-to-call phone, address,
  delivery note, every line with its options, subtotal, fee, tax, tip, total, mileage, and which driver/stop.
- Updated screens in `preview/`.

Delete `delivery.db` before running this build, the database schema changed.

- Drivers set their own stop stage: Received, At restaurant, En route, in one row on each stop, with the
  current stage highlighted. They can step back if they tapped too early, and Complete delivery appears
  once they are at the restaurant or en route. Dispatch still owns online/break status.

### Newest additions
- Driver groups on the dispatch board: On shift, Scheduled, Unavailable tabs, with one click to
  move any driver between groups (an on call driver becomes an active driver the moment you need them).
- Mass text: pick a group (or everyone) and send one message. It lands in each driver's dispatch chat and
  goes out by SMS when Twilio keys are set (TWILIO_SID, TWILIO_TOKEN, TWILIO_FROM).
- Drivers set their own availability by day and time in the driver app. Dispatch sees it on the driver card.
- Closed days calendar at /dispatch/hours: mark a day closed for one restaurant or for every store, with a
  reason. Closed days override normal hours everywhere, including the customer site.
- Any order can carry a dispatch note, visible to the driver and on the kitchen ticket.
- Clone an order from the board: same customer, address, items, fee and tip, sent out as a fresh pending order.

### Restaurant logins and roster changes

- Each store signs in at /restaurant/login with a store code and a PIN.
- The store can change its own name, store code, phone and PIN at /restaurant/account
  (Account button in the restaurant app header). The new pair works on the next sign in.
- Dispatch can reset any store login from /dispatch/manage with the "Edit login" button,
  and can add or delete restaurants from the same page. A delete is refused while the
  restaurant still has live orders, and it clears that store's menu, sub layers and choices.
- Store codes are unique, so a code already in use is refused with a message instead of
  silently locking the other store out.

### Mapping a new order

In the dispatch create-order screen, validating the address drops a pin on the customer,
an amber pin on the restaurant, and a dashed line between them, with the mileage and the
quoted delivery fee under the map plus a one-tap Google directions link.

### Roster groups

Drivers sit in one of two groups, Scheduled or Unavailable, and the board also has an
On shift tab for anyone currently online or on break. If you move an unavailable driver
onto shift anyway, their status reads "unscheduled driver" rather than "available", so
the board always shows you who is working outside the schedule.


## New order from an existing order

Clone is gone. Every order card has "New order from this" instead. It opens the create-order
page with the restaurant, every line item (custom ones included), the customer, address, note
and tip already filled in. Change anything you want, then place it: only then does it hit the
pending queue. The page asks what went wrong first:

1. Restaurant left an item off
2. Restaurant made the wrong item
3. Food was cold or wrong temperature
4. Driver delivered to the wrong address
5. Order never reached the customer
6. Order was damaged in transit
7. Order was too late
8. Other

Pick "Not a redo, just a new order" when it is simply a repeat. With a reason picked, the new
order carries a red "Redo of FF1234: ..." band on the dispatch card, on the kitchen ticket and
in the driver app, the original order is stamped with the same reason, and the event log
records it.

## Custom items and custom item fees

Both the create-order page and Edit on any order card take custom lines that are not on the
menu: name, price, an optional fee charged on top per unit, quantity and a note for the
kitchen. Custom item fees are totalled separately from the delivery fee and show as their own
line on the order, so a $40 catering tray with a $5 handling fee, times two, adds $80 of food
and $10 of fees.

## Sending a held order back out

Any order sitting in Pending with no driver has a driver picker and a Send to driver button.
Leave it on "Next up" to let dispatch pick, or choose a name to hand it to that driver.
Drivers who are not on shift are refused with a message.

## Putting it on a real website

It is one Python app, so it deploys once and the four portals are just four
addresses pointing at it. Nothing gets split into separate programs.

1. Push the folder to a GitHub repo.
2. Create a web service on Render (or Railway / Fly.io, same idea) from that repo.
   The included `render.yaml` and `Procfile` already set the start command:
   `gunicorn app:app --workers 1 --threads 12 --timeout 60 --bind 0.0.0.0:$PORT`
3. Add a disk and set `DB_PATH` to a path on it (for example `/var/data/delivery.db`),
   otherwise the database is wiped on every deploy. Set `SECRET_KEY` to a long random
   string and `TZ` to `America/Chicago`.
4. Buy a domain, point these four subdomains at the service, and add each one as a
   custom domain on the deployment:
   - `order.yourdomain.com` customers
   - `dispatch.yourdomain.com` dispatchers
   - `driver.yourdomain.com` drivers
   - `kitchen.yourdomain.com` restaurants
5. Set `SUBDOMAIN_ROUTING=1`. Each subdomain then lands on its own portal, while
   `/dispatch`, `/driver` and `/restaurant` still work on the plain URL too.

Keep `--workers 1 --threads 12` while the database is SQLite. One process writing
keeps things clean, and twelve threads is plenty for a town-sized operation. When
you outgrow that, raise the worker count and move the file onto a bigger disk.

Logins stay separate on their own: each portal has its own sign-in and its own
session, so a driver cannot open the dispatch board even by typing the URL.

## Who can see what

Each portal is sealed to the people who belong in it.

- Customers see the ordering site only. There are no staff links anywhere on it.
- Drivers see the driver app. Typing the dispatch or kitchen URL puts them back on their own run.
- Restaurants see their kitchen screen and nothing else.
- Dispatchers see the board, manage, schedule, map and account pages.

Anyone not signed in who types a staff URL gets that portal's sign-in page, and the
staff APIs answer 401 or 403 instead of returning data. Dispatchers no longer open the
kitchen screen directly; the same timer and ready controls are already on the board.


## Address drop-down

Both the customer checkout box and the dispatch new-order form suggest addresses as you type.
After four characters the field offers up to six matches; clicking one fills the box and prices
the delivery straight away. Suggestions come from Google if GOOGLE_MAPS_API_KEY is set, otherwise
from OpenStreetMap, and every suggestion is cached so the pick validates instantly.


## Why auto dispatch used to skip an order

Two settings fought each other: "one run at a time" and "unlimited stacking" were both on, and the
one-run rule was read first, so any driver already holding an order dropped out of the line and the
next order parked as "no driver available". Unlimited stacking now wins, and the queue says exactly
why an order is waiting: waiting on kitchen, address needs dispatch approval, auto dispatch off, or
no driver available. Dispatch also re-runs assignment when the kitchen sets or nudges a timer, when
a driver is moved on or off shift, when an order is reassigned by hand, and on every board refresh.


## Rotation

Auto dispatch hands out one order per driver, in turn. The driver at the front of the line takes the
first order, the next order goes to the next driver, and a driver who already has one waits until the
rotation comes back to them (they come back around when their run is finished, oldest wait first).
Nothing goes out automatically when only one driver is on shift, or none: those orders hold for the
dispatcher, who assigns and stacks them by hand. The queue names the reason on each held card:
no driver on shift, only one driver on shift, every driver has an order, waiting on kitchen,
address needs dispatch approval, or auto dispatch off.


## Dispatch board layout

The driver tabs and the dispatch chat sit in a rail on the right of the pending column and stay put
while the queue scrolls, so pending cards drag straight onto a driver and you can message them
without leaving the queue. Restaurants, mass text and blocked customers stay in the left column.


## Weekly availability

Drivers fill in a calendar for the coming week in the driver app: tick the days they can work, set
the hours on each, and send the week to dispatch. Every day lands as a pending request the
dispatcher approves or denies, the same as before.

Each week closes **Sunday at midnight**, the Sunday before that week starts. The driver app shows
the deadline and turns the banner red once it passes; a late week can still be sent, and it is
flagged as late in the dispatch log. "Copy my usual hours" fills the calendar from the driver's
standing pattern so a normal week takes one tap.

The dispatch schedule page has a Weekly submissions board: pick a week, see every driver's days
side by side, and see who has not sent theirs in yet. Standing hours still live below it.


## Opening day for the weekly schedule

The coming week's calendar opens on Friday by default and always closes Sunday at midnight. On the
dispatch schedule page, under Weekly submissions, you can change the day it opens every week, or set
a one-off date for just the upcoming week (handy for a holiday). Clear the one-off and it falls back
to the weekly day.

## Store hours

Every restaurant is seeded at 11:00 to 21:00, seven days. Change any store on the dispatch Store
hours page, and the 24h button and closed-days calendar still override it.

## Putting it online

It is one Flask app, so one deploy serves all four screens: the customer site at the root, then
/dispatch, /driver and /restaurant. Run it behind gunicorn on Render, Fly.io, Railway or a small VPS,
point your domain at it, and add subdomains (dispatch., driver., kitchen.) later if you want them
split. Keep the SQLite file on a persistent disk, and set GOOGLE_MAPS_API_KEY for real
driving miles. Https is required, which every host above gives you free.

## Phones: no app store, just the website

The driver and kitchen screens are phone-sized and installable straight from the browser, so drivers
and restaurants get an icon on the home screen with no store, no review, and no developer fees.

- iPhone: open https://yoursite.com/driver in Safari, tap Share, then Add to Home Screen.
- Android: open it in Chrome, tap the menu, then Install app.
- Kitchens do the same from https://yoursite.com/restaurant.

Once installed it opens full screen with no browser bar, keeps its own login, and updates the moment
you deploy, so nobody has to install anything again. The service worker caches only the layout, never
orders, so a driver who loses signal still gets the app shell and live data the second they are back.


## Driver phone calls

Each assigned stop in the driver app carries buttons to call the restaurant, call the
customer, and text the customer, next to the two navigation buttons. They use the phone's
own dialer, so nothing is routed through the app and no number needs typing.

## A pickup that is not on your list

Anywhere dispatch creates an order (Create an order, and every receipt on the Import
tab) the restaurant picker ends with "Not on the list, type the pickup in". Pick it and
you get three boxes: place name, pickup address and pickup phone.

* Mileage prices off the address you typed, so the fee is still first 3 miles then
  $1.00 a mile.
* The driver navigates to that address and the call button dials that number.
* The board, the order detail and the driver stop all show the typed name instead of a
  restaurant, so nobody has to guess where it is coming from.
* There is no kitchen app for a typed pickup, so it never waits on a restaurant to
  accept. Dispatch holds or sends it like any other order.

Nothing gets added to your restaurant list, and customers never see it on the site. If a
place becomes a regular, add it properly under Manage and it gets a menu and a login.

## Custom items

The Create an order screen has "Add a custom item": name, price, an optional per-unit
item fee and a kitchen note. Item fees total separately on the order as a custom item
fee, so they never get mixed up with the delivery fee.

Imported receipts have the same thing: each pending receipt has an Items block with
printed a total. Type nothing and the order carries one receipt line, exactly as before.

## Cropping a screenshot before it is read

picture on screen instead of being read straight away. Drag a box over the order details
with the mouse, then hit Capture and only that box is read. Drag again to redo the box.

* Capture reads the box you drew, at double size, which is why a tight crop on a small
  receipt reads far better than the whole screen.
* Read the whole picture skips the crop, the old behaviour.
* Skip this one drops that picture and moves to the next.
* Drop several at once and they queue up, one at a time, with a count of what is left.

The reading still happens on your own computer. No screenshot leaves the machine.


## Time zone

The app runs on Central time (America/Chicago) on its own, even when the host is on UTC. To use a different zone, set the `APP_TZ` variable. Check it any time at `/api/clock`.


## Card payments (typed in, nothing saved)

Stripe is gone. On any card order, dispatch clicks **Card** on the order (or the card box pops up right after you place an order on the new-order page). Type the name, number, expiration, CVC and billing ZIP, use the Copy buttons (or Copy everything) to paste them into whatever card terminal you use, run it there, then hit **It went through: mark paid**.

The card box lives only in that browser tab. The number is never sent to the server and never saved; it clears when you close the box, after 10 idle minutes, or when you leave the page. The order keeps only "card ending 1234" and the approval number you type. Run refunds on your card terminal too, then record them with Refund on the order.

If dispatch adds an item or fee after the order was paid, the board shows "balance due" with a **Card for $X** button for the difference.

Cash orders work as before: flip the Cash switch, the driver sees "Collect cash", and the order is marked paid on delivery.

### Customer cards on the website

Customers type their card at checkout, and it's checked the same way as the dispatch card box (name, number, expiration, CVC, ZIP). A card order goes to Pending as "card on file, run it". Dispatch clicks **Run card**, and the card box opens already filled in. Copy it into your outside terminal, run it, then hit **It went through: mark paid**. The order then moves to the kitchen and the driver queue.

The card is encrypted on the server and only dispatch can open it (each opening is logged). When the order is marked paid, the security code is deleted and the name, number, expiration and ZIP stay encrypted so dispatch can click **View card** on the order later, even after it's complete. Paid cards are deleted after 30 days (`CARD_KEEP_DAYS`), unpaid cards after 24 hours (`CARD_HOLD_HOURS`), and a cancelled order's card right away. A card typed into the dispatch card box is kept when you close the box. Set a `CARD_KEY` variable (or at least `SECRET_KEY`) on your host and don't change it, or cards still waiting can't be read. Keeping card numbers on your server, even briefly, puts you under PCI card-security rules. Ask your card processor which self-assessment applies to you.



## Card payments and the restaurant
- A card order does not reach the restaurant or a driver until dispatch marks it paid. A timer you set before that is saved and starts when it is paid. Mark ready is blocked until then.
- Cash orders go straight to the restaurant. Switching an order to cash releases it; switching back to card pulls it back if the kitchen has not started.
- If the card will not go through, use "Card failed: cancel" on the order or "Card would not go through" in the card box. The order is cancelled and the card is deleted.
- An unpaid card order left for CARD_HOLD_HOURS (24 by default) is cancelled automatically.


## Business name, address and phone
Dispatch > Settings (top menu) holds the business name, business address and dispatch phone number.
The name shows at the top of every page; the customer site footer shows all three. The phone is
the number drivers and restaurants tap to call dispatch.


## Driver location and address

When a driver taps Call dispatch, their phone sends its GPS fix and the board shows the closest street address under the call ("Near: 908 Avenue B, Opelika, AL 36801"). While that call is open the phone checks in every 8 seconds, so the address keeps updating until a dispatcher taps Done.

The Driver map page (Dispatch > Map) refreshes every 5 seconds and shows each on-shift driver's closest address, updated as they move.

Addresses come from OpenStreetMap for free (about one lookup a second, cached every ~10 metres). For a bigger fleet set GOOGLE_MAPS_API_KEY in the host's environment and Google is used instead. Drivers must allow location in their browser, and nothing is tracked while they are off shift.


## Open and close the business
The dispatch board has Open Business and Close buttons at the top. While closed, every driver sees "<business name> is closed." in place of the app, and the app opens by itself when dispatch presses Open Business. A driver who is offline and not on today's schedule sees "<business name> does not have you scheduled. Call dispatch or message them below to be put online." with a Call dispatch button and the dispatch chat, so they can still reach you. The name comes from Dispatch > Settings.

## Completed tabs
Drivers and restaurants each have a Completed tab. Finished orders stay there until dispatch presses Close at the end of the day, then both start fresh. The restaurant can also pick an earlier day.


## Completed by day, GPS log, Excel export
- Dispatch board > Completed orders: pick a day (or Previous/Next day). Shows that day's delivered and cancelled orders with a total.
- Export this day to Excel, or pick From/To and Export range. The workbook has Orders, Summary and By driver sheets.
- Dispatch > GPS log: pick a driver and dates. Shows every status the driver marks (requests, Received, At restaurant, On the way, Delivered), every status dispatch sets, and GPS points about once a minute while online or on break, each with the address and a Maps link. Route in Maps draws the day's path. Export to Excel from the same page. Kept 90 days (env GPS_LOG_KEEP_DAYS).
- requirements.txt now includes openpyxl. Render/Railway install it on the next deploy.
