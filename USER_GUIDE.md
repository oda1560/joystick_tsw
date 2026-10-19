# TSW Joystick User Guide

Last updated 2026-10-18

## What it does

The TSW7 Joystick Bridge lets you drive Train Sim World 7 with a flight stick: push forward for power, pull back to brake, and work the reverser, AWS, alerter and doors from the stick's buttons. It was built around the Logitech Extreme 3D Pro, but any joystick works once its axes and buttons are assigned.

Two tools work without driving: **Route maps** prints a speed-limit map for any service or a whole route, and the **train checker** tests the bridge against every train you own.

The bridge never uses the emergency brake, and it only sends values when you move the stick, so the keyboard and mouse keep working alongside it.

## Before you start

You need Windows, Train Sim World 7 from Steam, Python 3 with pygame, and the game started with `-HTTPAPI`. Set-up takes about five minutes and is done once.

| What | Why | How |
| --- | --- | --- |
| Windows 10 or 11 | The bridge uses Windows for the look-around mouse and auto-start | — |
| Train Sim World 7 (Steam) | The bridge reads each train's controls from the game's files, found through Steam | Any Steam library folder works |
| Python 3 (tested on 3.12) | Runs the bridge | [python.org](https://www.python.org/downloads/); tick "Add python.exe to PATH" |
| pygame (tested on 2.6) | Reads the joystick | `pip install pygame` |
| A joystick | Throttle and brake, reverser, buttons | Plug it in before or after starting; the bridge waits for it |

### Turn on the game's API

The bridge talks to the game through Train Sim World's External Interface, which is off unless you ask for it.

1. In Steam, right-click **Train Sim World 7** and choose **Properties**.
2. Under **General → Launch options**, type `-HTTPAPI`.
3. Start the game once. It writes a key file, `CommAPIKey.txt`, that the bridge reads.

The bridge looks for that key in `OneDrive\Documents\My Games\TrainSimWorld7\Saved\Config`.

### Game settings worth knowing

- **AI door control**: with the game's AI guard working the doors, the guard opens and closes doors on its own. The bridge's door buttons still work, but the guard's own door near the cab ignores them and closes by itself just before departure.

## Starting the bridge

Double-click **Start TSW Joystick.bat** in the project folder, then start the game and get into a cab. The order doesn't matter: the bridge waits for the joystick, the game and a train, and picks each up as it appears.

| Launcher | What it does |
| --- | --- |
| Start TSW Joystick.bat | Opens the bridge window. The one to use. |
| Auto-start with TSW - on.bat | From now on, opens the bridge window whenever the game starts and closes it when the game exits. Survives a restart of Windows. |
| Auto-start with TSW - off.bat | Turns auto-start off again. |
| Start TSW Joystick (console).bat | A plain text version for testing. It uses the built-in defaults, not the buttons and axes set in the window. |
| Route maps.bat | Opens the Route maps window (see Offline tools). |
| Print a route map.bat | The same maps from a text prompt. |

With auto-start on, closing the bridge window while you play keeps it closed until the next time the game starts. If you already opened a window by hand, auto-start doesn't open a second one.

### The window

The window has five parts, top to bottom:

1. **Header**: the green **ACTIVE** button. Click it to pause (it turns amber, **PAUSED**); click again to resume. A high beep means active, a low one paused.
2. **Status**: a dot for the joystick, the game and the train, green when all is well. Under them, what the bridge found in this cab: throttle and brake (or one combined lever), reverser, AWS, alerter and doors.
3. **Live**: the stick's position, each lever's place in the cab, and a readout such as POWER 40%, BRAKE 75% or NEUTRAL. "Sending to game" means the stick is driving; "Preview" means it isn't connected yet. On the right, the reverser as R / N / F (filled = the train, outlined = your slider) and the AWS and alerter lights, lit while their buttons are held. A bar at the bottom shows where you are looking.
4. **Settings**: axes and buttons (see Settings).
5. **Log**: the last few things that happened. The same lines go to `bridge.log`.

### Pausing and closing

While paused the bridge lets go of every handle and ignores the stick and buttons, so you can drive from the keyboard alone. Start paused with `pythonw tsw_joystick_ui.py --paused`.

Closing the window lets go of any handle the bridge was holding, so nothing is left stuck in the cab.

## Joystick controls

The stick drives power and brake, the base slider sets the reverser, the twist looks around, and the buttons work the cab's safety systems and doors. The defaults below are for a Logitech Extreme 3D Pro, with buttons numbered as printed on the stick; every one can be changed in Settings.

| Control | Default | What it does |
| --- | --- | --- |
| Stick forward | Axis 1 | Power, from Off to full |
| Stick centre | Axis 1 | Off: no power, brake released. The dead zone (8%) keeps it there |
| Stick back | Axis 1 | Train brake, from released to full service. Never emergency |
| Base slider | Axis 3 | Reverser: + end Forward, middle Neutral, − end Reverse |
| Twist | Axis 2 | Look left or right; centre it and the view returns straight ahead |
| Trigger | Button 1 | AWS acknowledge |
| Thumb button | Button 2 | Alerter, DSD, vigilance or SIFA acknowledge |
| Base buttons | 7 / 8 | Open the left / right doors |
| Base buttons | 9 / 10 | Close the left / right doors |
| Pause button | Not set | Pauses and resumes the bridge |

### Power and brake

- **Separate throttle and brake**: forward moves the throttle, back moves the brake, and the other handle sits at Off.
- **One combined lever** (Class 331, 350, 170…): the stick moves the one lever through power, Off and the brake steps.
- **Release / Hold / Apply brake valves** (Class 66 and similar): the back travel is split in thirds, release, hold and apply, and the valve stays in Apply for as long as you hold the stick there.
- The full ends of the stick give exactly full power and full brake. Emergency positions are always left out.
- The bridge reads each train's handle positions from the game's files. If it can't, it estimates them and says so in the status line. The first time a combined lever comes back to Off, the bridge checks where Off really is and remembers it for that train.

### Handle speed

Some trains jump through notches if you flick the stick. The **Handle speed** slider (in a train) sets how long the handle takes from Off to full, up to 4 s, and the handle follows the stick no faster. It is kept per train class; "instant" follows the stick at once.

### Reverser

The reverser only moves when the slider enters a new zone, and only while the train is below about 1 km/h. It never goes to Off. On trains with no Neutral, the bridge uses the nearest stand-in, such as Engine Only or Secure. If the reverser doesn't follow, the log says so: the train may need the master key in or the brake applied first.

### Buttons

Cab buttons are held down in the cab for as long as you hold the joystick button, like a hand on the button. Doors won't open while the train is moving. On the Class 142 and the Class 323 the door buttons only work with the door key switch On: the bridge turns it On for the press and back Off after, as the game does. With the game's AI guard on, the guard's own door near the cab ignores the close buttons and closes by itself.

### Looking around

Twist the stick to turn the view up to ±90° (30–150° in Settings). It works only while the game is the active window and no mouse cursor is showing, and the bar at the bottom of the window says why when it isn't steering. Turn it on or off with **Use twist**.

### The keyboard still works

The bridge only sends a value when you move the stick, so the keyboard and mouse can still drive. After you get into a train it waits for the stick to move before touching any lever. On locos with a cab at each end, change ends and the bridge follows within about 2 seconds.

## Offline tools

Two tools read the game's files directly and work with the game closed: route maps to learn a route from, and a checker that tests the bridge on every train you own.

### Route maps

A route map is a printable A4 landscape page set for one service: an overview map and route card, then strip diagrams of the line with its speed profile, limit boards, platforms, signals and distances. Use it to learn a route the way a real driver would, instead of relying on the game's speed limit hints.

A **whole-route map** covers every line the route's passenger journeys use, for any service, in a few pages (Airedale & Wharfedale: 6). Each strip shows both ways: above the track the limits going left to right (▶), below it going right to left (◀), each with its own boards and signals. The first page has a map of the sections and which page each is on; a numbered circle on the track marks where another section joins or leaves. The map has a board at each place to slow down, so the first page on its own is enough to print: where the limit is 45 mph (70 km/h) or less (red edge), or 20 mph (30 km/h) or more below the limit either side. A board gives the lower limit of the two ways on the coloured stretch it points to; one with an arrow is only for trains going that way. Grey numbers give the limit along long stretches. Every station the journeys pass is named, even one whose platforms are on a track the map doesn't draw (the slow lines of a four-track main line, say). Where names and boards are too crowded to read, the place is framed with a letter and drawn again larger in an empty part of the page, in a box with the same letter, its scale and how much larger it is. Where the page has room left, so is every other place where stations or boards are close together (a station with a few boards round it, say). Sections under a mile (or a kilometre) long, loops and crossovers, aren't numbered on the map; the key lists them on one line. The map also shows landmarks from the route's own files: tunnels (named where the route names them), viaducts and rail bridges, roads over the line, level crossings, rivers, canals and lakes, the sea, and depots and junctions. Every level crossing is drawn; a road bridge too close to another or to a level crossing to tell apart is left off the main map (an inset may still show it). Rivers and lakes are drawn where the route has water: routes model it only near the line, so a river often shows as a short stretch where it passes, and small ponds are left off. An unnamed tunnel is found from its two ends, so two short tunnels close together can show as one, and some routes model farm and foot crossings like level crossings. The sea's outline comes from the landscape in pieces about 250 m across, so its edge is rough, and it's drawn only on routes whose files have a sea at sea level. It leaves out what isn't a passenger line (depot moves, freight-only track, crossovers and other short bits), and where there's more than one track each way it shows the one most journeys use, so a service taking another platform road can meet a limit the map doesn't show. Your own stops aren't marked.

Under the first page's map, faintly, are the real roads and towns from OpenStreetMap, to tell where you are between stations: motorways, A and B roads, the numbers of motorways and A roads, built-up areas, rivers, canals and lakes, other railways, and the names of towns and villages. Names and road numbers go only where the page has room, so some are left off (a town under the key, say), and a town named like a station near it isn't named again. On a long route, whose map is smaller, B roads and villages are left out. The insets stay as they were. The first whole-route map of a route downloads them, which adds about half a minute; they're kept in `street_maps` after that. With no internet the map is made without them, and so it is when the route's track isn't where OpenStreetMap's railways are (the route's world would be laid out in a way the tool doesn't know). The footer of the first page credits OpenStreetMap, as its licence asks.

To see whole-route maps without making them, download [maps.zip](https://github.com/oda1560/joystick_tsw/releases/latest/download/maps.zip) from the project's latest release on GitHub. It has a ready-made map for each of a few dozen routes: unzip it and open one in your browser. You don't need the game or the bridge for that.

1. Double-click **Route maps.bat**.
2. Pick a **Route**, then a **Journey**. Journeys are grouped by where they start and stop; tick **Short moves** to list depot and other short moves too.
3. Pick a **Service** if the journey has several, then click **MAKE MAP**. Or click **WHOLE ROUTE** for the whole-route map (it takes up to a minute or so: it reads the route's scenery for the landmarks, and the first time on a route it downloads the roads and towns).
4. The map opens in your browser, ready to print.

In a cab, click **Map the service I'm driving** to map the service you're on now. Maps are saved in the folder shown under **Save to** (at first `route_maps` in the project folder); **Change…** picks another and **Default** goes back. Maps made before are listed: double-click one to open it again.

From a text prompt, **Print a route map.bat** does the same, or:

```bash
python tsw_routemap.py airedale 2S00
```

For the whole route:

```bash
python tsw_routemap.py airedale all
```

The limits are those for passenger trains. A limit board marks where the front of the train meets the limit.

### Train checker

The checker runs the bridge against a model of every installed train, built from the game files, without loading any of them. It pushes the stick to full power, centre and full brake, tries the reverser and presses each cab button, then writes what happened to `train_check_report.txt`, with anything doubtful flagged. Locos with a cab at each end are checked from each cab. Steam locomotives are left out.

| Command | What it checks |
| --- | --- |
| `python tsw_check.py` | Every train |
| `python tsw_check.py 331 802` | Only trains whose name contains 331 or 802 |
| `python tsw_check.py --no-files` | How the bridge works when it can't read the game files and has to estimate |
| `python tsw_check.py --live` | The train you're in now, model against game. Read-only: it moves nothing |

The model assumes the cab is set up and ready to drive, so it can't catch interlocks the train applies itself, such as no power with the reverser in Neutral.

## Settings

Everything is set in the bridge window and saved as soon as you change it. The axis numbers next to **Axis** move live as you move the stick, which helps you find the right one.

| Setting | What it does |
| --- | --- |
| Joystick | Which device to use, if more than one is plugged in |
| Axis · Invert | The power and brake axis. Tick Invert if forward brakes |
| Dead zone: Stick | How far the stick moves from centre before anything happens (0–30%) |
| Dead zone: Twist | The same for the twist (0–50%) |
| Handle speed | Per train class: how long the handle takes from Off to full (instant to 4 s) |
| Reverser: Use slider · axis · Invert | Turns slider control of the reverser on or off, and which axis it is |
| Look: Use twist · axis · Invert · angle | Turns twist look on or off, and how far full twist turns the view (±30–150°) |
| Look smoothing | Quick to Smooth: how gently the view follows the twist |
| AWS, Alerter, Open / Close doors, Pause button | Click **Assign**, then press the joystick button. **Clear** unsets it |
| Keep this window on top | Keeps the bridge window over other windows |
| Re-detect train | Looks at the cab's controls again |

One joystick button can do only one thing: assigning a button that already has a job takes it off the old one.

### Files the bridge keeps

All of these sit in the project folder and are left out of git. Deleting one starts that part afresh.

| File | Holds |
| --- | --- |
| `settings.json` | The window's settings |
| `handle_speed.json` | Handle speed for each train class |
| `lever_calibration.json` | Where Off and full brake really are on combined levers, learned per train |
| `routemap_settings.json` | The Route maps save folder |
| `bridge.log`, `bridge.log.1` | The log. A new file starts past 1 MB, keeping one older |
| `error.log` | Written only if the window fails to start |
| `train_check_report.txt` | The train checker's last report |
| `street_maps` (folder) | The roads and towns from OpenStreetMap under each route's whole-route map, kept after the first download |

## Troubleshooting

Read `bridge.log` in the project folder first. Each run starts with a "bridge started" line, and the `Train:` line shows what the bridge found in the cab.

| What you see | What to do |
| --- | --- |
| Game: "No API key - start TSW7 with -HTTPAPI…" | Add `-HTTPAPI` to the game's launch options and start it once. The key must be in `OneDrive\Documents\My Games\TrainSimWorld7\Saved\Config`; if your Documents folder isn't in OneDrive, the bridge won't find it |
| Game: "Game not reachable…" | The game isn't running, or was started without `-HTTPAPI` |
| Game: "Game API error 403" | The key changed. The bridge reloads it by itself; restart the game if it stays |
| Game: "Connected - not in a cab" | Get into the driver's seat |
| Train: "no throttle/brake found" | Click **Re-detect train**. If that fails, run `python tsw_joystick.py --list` in that cab and keep the output |
| "handle places estimated" | The game files couldn't be read, or don't have this train. Driving still works, but less exactly |
| "reverser not found", or "No answer from … looking at the train again" | The game didn't answer during set-up. The bridge tries up to 5 more times; click **Re-detect train** if it gives up |
| "Reverser is at …, not …" | The train refused the move: put the master key in or apply the brake first |
| "Reverser NOT moved" or "Doors NOT opened: train is moving" | Stop first: both wait until the train is below about 1 km/h |
| One door won't close | The game's AI guard keeps its own door open until just before departure |
| Stick forward brakes | Tick **Invert** next to Axis |
| Joystick: "Not found" | Plug it in, then pick it under **Joystick**. `python tsw_joystick.py --axes` shows its raw axis values |
| Look bar: "game not focused" or "cursor active" | Click into the game, and close any menu or mouse cursor |
| Whole-route map: "No roads and towns on the map: …" | It says why: no internet (try again later; the map is fine without them), or the route's track isn't where OpenStreetMap's railways are |
| The window doesn't open | Look in `error.log`, or start **Start TSW Joystick (console).bat** to see the error |

For developers: the automated tests need no game and open no windows. Run them from the project folder:

```bash
python -m unittest discover tests
```
