# ResyGrabber - Restaurant Reservation Bot

\*THIS USED TO BE CLOSED SOURCE, BUT I QUICKLY VIBECODED AN OPEN SOURCE VERSION WITHOUT LICENSING LOGIC. I ALSO QUICKLY MERGED TWO REPOS INTO THIS ONE. PLEASE LET ME KNOW IF ANY ISSUES ARE ENCOUNTERED, BUT YOU CAN AT LEAST EXAMINE THE CODE. FEEL FREE TO REACH OUT TO LEARN MORE ABOUT HOW I CREATED THIS!

This is an open-source tool to help manage restaurant reservations on Resy.com. It was previously a SaaS product but has been converted to a locally runnable application with no authentication required. This is the first SAAS product that I've released and it was very fun to work on! I am open sourcing this now though because New York passed laws making it illegal to sell dinner reservations.

## Features

- Create and manage reservation tasks
- Use proxies to avoid IP blocks
- Schedule tasks to run at specific times
- Automatically book reservations when they become available
- Bypass captchas by using an undocumented/deprecated API endpoint

## Setup and Installation

### Requirements

- Python 3.7 or higher
- pip (Python package manager)

### Installation

1. Clone this repository:

```
git clone https://github.com/yourusername/resybot-open.git
cd resybot-open
```

2. Install the dependencies for both client and server:

```
cd client
pip install -r requirements.txt
cd ../server
pip install -r requirements.txt
```

## Running the Application

### Simple Start (Recommended)

To start both the server and client with a single command:

```
python start.py
```

This will launch both the server and client components automatically.

### Manual Start

Alternatively, you can start each component separately:

1. Start the Server:

```
cd server
python server.py
```

2. Start the Client (in a separate terminal):

```
cd client
python entry.py
```

The client will launch a menu-driven interface where you can:

- Add and manage reservation tasks
- Configure proxies
- Manage Resy.com accounts
- View and cancel existing reservations
- Schedule and run tasks

## Configuration

### Adding Resy.com Accounts

1. From the main menu, select "4) Manage Accounts"
2. Follow the prompts to add your Resy.com account information
   - You'll need your Auth Token and Payment ID from Resy.com

### Proxies (Optional for Reservation Tasks)

Leave the proxy list empty to use the default network connection for immediate or
scheduled reservation tasks, viewing reservations, and cancellation. Standard HTTP
proxy environment variables still apply if configured on your machine. Existing
configured proxies continue to work.

The separate account-generation feature is unchanged and still requires a proxy.
This setting does not change CAPTCHA handling or override platform rate limits.

### Creating Tasks

1. From the main menu, select "1) Show tasks"
2. Select "a) Add task"
3. Follow the prompts to create a new reservation task

### Running Tasks

1. From the main menu, select "7) Start Tasks"

This starts **all saved tasks**. Reservation tasks do not require an `info.json`
file or solver keys; missing Discord configuration disables Discord notifications.
This does not enable CAPTCHA solving in the booking worker. Startup prints the task
count and waits for Enter on failure so the menu does not immediately erase the error.
Restart the client after updating its code.

## Offline Tests

Run `python3 -m unittest discover -s tests -v` from the repository root. These tests
use mocked dependencies and do not contact Resy, load account files, or book anything.

## Contributing

Contributions are welcome! Please feel free to submit a Pull Request.

## Open Source Changes

This project was previously a SaaS product with the following licensing/authentication requirements:

- Required a valid license key through Whop
- Client connected to a remote server hosted on Railway
- HWID validation to prevent license key sharing

The following changes were made to open source the project:

- Removed all licensing and authentication requirements
- Configured the client to connect to a local server instead of the remote server
- Added a simple startup script to run both client and server with one command
- Updated documentation with clear instructions for local usage

## License

This project is licensed under the MIT License - see the LICENSE file for details.
