# TSW Joystick User Guide

Last updated 2026-10-10

## What it does

The TSW7 Joystick Bridge lets you drive Train Sim World 7 with a flight stick: push forward for power, pull back to brake, and work the reverser, AWS, alerter and doors from the stick's buttons. It was built around the Logitech Extreme 3D Pro, but any joystick works once its axes and buttons are assigned.

It also shows small panels over the game while you drive:

- **Next stop**: hold a button to see how far the next stop is, and the speed limit now.
- **At a stop**: whether to wait, shut the doors or depart, with the times, your points and a short ride report.
- **Speeding**: the limit pops up by itself when you go over it.
- **Passenger comfort**: a warning on firm or harsh braking and acceleration, and a prompt to ease off the brake just before you stop.
- **End of service**: how the run went against your best before.

Two tools work without driving: **Route maps** prints a speed-limit map for any service, and the **train checker** tests the bridge against every train you own.

The bridge never uses the emergency brake, and it only sends values when you move the stick, so the keyboard and mouse keep working alongside it.

## Before you start

You need Windows, Train Sim World 7 from Steam, Python 3 with pygame, and the game started with `-HTTPAPI`. Set-up takes about five minutes and is done once.

| What | Why | How |
| --- | --- | --- |
| Windows 10 or 11 | The bridge uses Windows for the overlays, the look-around mouse and auto-start | — |
| Train Sim World 7 (Steam) | The bridge reads each train's controls from the game's files, found through Steam | Any Steam library folder works |
| Python 3 (tested on 3.12) | Runs the bridge | [python.org](https://www.python.org/downloads/); tick "Add python.exe to PATH" |
| pygame (tested on 2.6) | Reads the joystick | `pip install pygame` |
| A joystick | Throttle and brake, reverser, buttons | Plug it in before or after starting; the bridge waits for it |

### Turn on the game's API

The bridge talks to the game through Train Sim World's External Interface, which is off unless you ask for it.

1. In Steam, right-click **Train Sim World 7** and choose **Properties**.
2. Under **General → Launch options**, type `-HTTPAPI`.
3. Start the game once. It writes a key file, `CommAPIKey.txt`, that the bridge reads.

The bridge looks for that key in `OneDrive\Documents\My Games\TrainSimWorld7\Saved\Config`. Points and the service history come from the save games next to it, in `Saved\SaveGames`.

### Game settings worth knowing

- **AI door control**: with the game's AI guard working the doors, the guard opens and closes doors on its own. The bridge's door buttons still work, but the guard's own door near the cab ignores them and closes by itself just before departure.
- **Display mode**: the panels are drawn on top of the game window. If they don't appear over fullscreen, switch the game to borderless windowed.

## Starting the bridge

Double-click **Start TSW Joystick.bat** in the project folder, then start the game and get into a cab. The order doesn't matter: the bridge waits for the joystick, the game and a train, and picks each up as it appears.

| Launcher | What it does |
| --- | --- |
| Start TSW Joystick.bat | Opens the bridge window, with the panels over the game. The one to use. |
| Auto-start with TSW - on.bat | From now on, opens the bridge window whenever the game starts and closes it when the game exits. Survives a restart of Windows. |
| Auto-start with TSW - off.bat | Turns auto-start off again. |
| Start TSW Joystick (console).bat | A plain text version for testing. It uses the built-in defaults, not the buttons and axes set in the window, and shows no panels. |
| Route maps.bat | Opens the Route maps window (see Offline tools). |
| Print a route map.bat | The same maps from a text prompt. |

With auto-start on, closing the bridge window while you play keeps it closed until the next time the game starts. If you already opened a window by hand, auto-start doesn't open a second one.

### The window

The window has five parts, top to bottom:

1. **Header**: the green **ACTIVE** button. Click it to pause (it turns amber, **PAUSED**); click again to resume. A high beep means active, a low one paused.
2. **Status**: a dot for the joystick, the game and the train, green when all is well. Under them, what the bridge found in this cab: throttle and brake (or one combined lever), reverser, AWS, alerter and doors.
3. **Live**: the stick's position, each lever's place in the cab, and a readout such as POWER 40%, BRAKE 75% or NEUTRAL. "Sending to game" means the stick is driving; "Preview" means it isn't connected yet. On the right, the reverser as R / N / F (filled = the train, outlined = your slider) and the AWS and alerter lights, lit while their buttons are held. A bar at the bottom shows where you are looking.
4. **Settings**: axes, buttons and panel switches (see Settings).
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
| Base button | 11 | Hold to show the next stop (see On-screen panels) |
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

Cab buttons are held down in the cab for as long as you hold the joystick button, like a hand on the button. Doors won't open while the train is moving. With the game's AI guard on, the guard's own door near the cab ignores the close buttons and closes by itself.

### Looking around

Twist the stick to turn the view up to ±90° (30–150° in Settings). It works only while the game is the active window and no mouse cursor is showing, and the bar at the bottom of the window says why when it isn't steering. Turn it on or off with **Use twist**.

### The keyboard still works

The bridge only sends a value when you move the stick, so the keyboard and mouse can still drive. After you get into a train it waits for the stick to move before touching any lever. On locos with a cab at each end, change ends and the bridge follows within about 2 seconds.

## On-screen panels

Five panels appear over the game by themselves, or while you hold a button. They never take focus from the game, and clicks pass through them.

| Panel | Where | When it shows | Turn off with |
| --- | --- | --- | --- |
| Next stop | Top centre | While you hold button 11 | Clear the Next stop button |
| What to do at a stop, with the schedule | Top right | While stopped at a stop, and 30 s after leaving | Show what to do at stops |
| Speeding | Under the next stop panel | While over the limit | Show the limit when speeding |
| Passenger comfort | Under the speeding panel | Firm or harsh braking or acceleration, and the last seconds of a stop | Show passenger comfort |
| Service complete | Top right, in place of the schedule | At the last stop of a service | Show what to do at stops |

Distances and speeds follow where the train is: miles, yards and mph in Great Britain, Ireland and the US, kilometres, metres and km/h elsewhere.

### Next stop

Hold button 11 to see how far the next stop is; let go and it hides.

- **Big number**: distance to the next stop or go-via. It turns amber once you've run past the marker. A "≈" means an estimate: on routes whose timetable data has no signal references, it's counted from the last stop.
- **Next line**: "Stop at Shipley" or "Go via …".
- **After a go-via**: the stop after it, and how far that is.
- **Last line**: the speed limit now, amber or red while you're over it.

### What to do at a stop

When you stop at a timetabled stop, a panel in the top right tells you what to do next.

| It says | It means |
| --- | --- |
| WAIT | It isn't departure time yet. Shows the time now, the time left and whether the doors are open |
| SHUT THE DOORS | 30 s or less to departure with the doors open, or the game has let you go with the doors still open. Red once you're late |
| WAIT FOR THE SIGNAL | The game has let you go, but the signal ahead is at danger within 400 m |
| DEPART | Go. The line under it gives the next stop and its distance |
| DEPARTED | Stays up 30 s after you move off, counting down to the next stop |
| END OF SERVICE | The last stop of the service |

Below that are the station and its departure time, your points with the change from this stop, how early or late you arrived, and how far from the marker you stopped. Points come from the game's own save, which it writes as you arrive and leave, so they can take a moment to appear.

Under the panel, the **schedule** lists this stop and the next four, with platforms and arrival and departure times, then how many stops are left to the terminus.

### Speeding

Go more than about 1 km/h over the limit and a panel shows the limit and your speed, for example "SPEED LIMIT 60 mph · you 67 mph". It's **amber** while you're within the game's own tolerance (3 mph on Airedale–Wharfedale) and **red** once the game counts it as speeding and takes points.

### Passenger comfort

The comfort panel warns when the ride gets rough for passengers. It judges the force passengers feel, corrected for the gradient.

| It says | When | What to do |
| --- | --- | --- |
| FIRM BRAKING / FIRM ACCELERATION (amber) | Over 0.9 m/s² | Ease off a step, or ease off the power |
| HARSH BRAKING / HARSH ACCELERATION (red) | Over 1.2 m/s², with the number of passengers aboard | Ease off now |
| STOPPING IN n s · EASE OFF THE BRAKE | Braking over 0.6 m/s² and about to stop | Ease the brake off before the train stops |
| SMOOTH STOP or JOLT ON STOPPING | For 6 s after stopping. A jolt is braking over 0.7 m/s² in the last 1 m/s | — |

The ease-off warning comes early enough for you to react and for the brake to release. The bridge times how long each train's brake takes to ease off and uses that from then on; until it has, it allows 3 s.

At the next stop, the what-to-do panel adds a **ride report** for the run since the last station: a smooth stop or a jolt, any harsh moments, passengers aboard, and "smooth rides n of m this service". Comfort is judged on passenger trains only, empty ones included; freight and light engines stay quiet.

### End of service and service history

At the last stop, a **Service complete** table compares this run with your best run of the same service before: points, stops on time, stop accuracy, smooth rides and passengers. Better values are green. Arriving up to 60 s late counts as on time, and so does arriving early.

Click **Service history** in Settings to see every service you've driven to the end, newest first, with each service's best run in green. Restart the bridge at the terminus and it updates the same run rather than adding a new one.

## Offline tools

Two tools read the game's files directly and work with the game closed: route maps to learn a route from, and a checker that tests the bridge on every train you own.

### Route maps

A route map is a printable A4 landscape page set for one service: an overview map and route card, then strip diagrams of the line with its speed profile, limit boards, platforms, signals and distances. Use it to learn a route the way a real driver would, instead of relying on the game's speed limit hints.

1. Double-click **Route maps.bat**.
2. Pick a **Route**, then a **Journey**. Journeys are grouped by where they start and stop; tick **Short moves** to list depot and other short moves too.
3. Pick a **Service** if the journey has several, then click **MAKE MAP**.
4. The map opens in your browser, ready to print.

In a cab, click **Map the service I'm driving** to map the service you're on now. Maps are saved in the folder shown under **Save to** (at first `route_maps` in the project folder); **Change…** picks another and **Default** goes back. Maps made before are listed: double-click one to open it again.

From a text prompt, **Print a route map.bat** does the same, or:

```bash
python tsw_routemap.py airedale 2S00
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
| AWS, Alerter, Open / Close doors, Next stop, Pause button | Click **Assign**, then press the joystick button. **Clear** unsets it |
| Keep this window on top | Keeps the bridge window over other windows |
| Show the limit when speeding | The speeding panel |
| Show what to do at stops | The stop, schedule and service complete panels |
| Show passenger comfort | The comfort panel and the ride report |
| Re-detect train | Looks at the cab's controls again |
| Service history | Opens the list of services driven to the end |

One joystick button can do only one thing: assigning a button that already has a job takes it off the old one.

### Files the bridge keeps

All of these sit in the project folder and are left out of git. Deleting one starts that part afresh.

| File | Holds |
| --- | --- |
| `settings.json` | The window's settings |
| `handle_speed.json` | Handle speed for each train class |
| `lever_calibration.json` | Where Off and full brake really are on combined levers, learned per train |
| `brake_ease.json` | How long each train's brake takes to ease off, for the comfort warning |
| `service_history.json` | Services driven to the end. A damaged file is moved to `.bad`, not overwritten |
| `routemap_settings.json` | The Route maps save folder |
| `bridge.log`, `bridge.log.1` | The log. A new file starts past 1 MB, keeping one older |
| `error.log` | Written only if the window fails to start |
| `train_check_report.txt` | The train checker's last report |

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
| Panels don't appear over the game | Switch the game to borderless windowed |
| Next stop shows "≈" | This route's timetable data has no signal references, so the distance is an estimate from the last stop |
| No points on the stop panel | Points come from the game's save, written as you arrive. Wait a moment |
| The window doesn't open | Look in `error.log`, or start **Start TSW Joystick (console).bat** to see the error |

For developers: the automated tests need no game and open no windows. Run them from the project folder:

```bash
python -m unittest discover tests
```
