Minecraft Discord Bridge

Required Python version: 3.6 or later.

Docker is not required to run this bot but its use is strongly encouraged for ease of management.
Installation instructions

    Clone the repository.

    Edit the config.json file to your liking, an example is provided in config.example.json.

    mv config.example.json config.json
    vi config.json

    If you are using an sqlite database (like the default one in the config, create it now).

    touch db.sqlite

    Run the bridge!

        If you are not using docker, just install the dependencies and start the bot.

        python -m pipenv --three
        python -m pipenv install
        python -m pipenv run ./webhook-bridge.py

        If you are using docker, just use the included docker-compose.yml to get up and running.

        docker-compose up -d

    If this is the first time you run this bridge, you need to invite it to your discord guild with the following link:

    Note: Make sure to replace the "<CLIENT_ID>" in the link below with your bot's client ID

    https://discordapp.com/oauth2/authorize?client_id=<CLIENT_ID>&scope=bot&permissions=536879104

    Once it has joined your server, create a text channel for it to post in, then press the "Edit Channel" button on that channel. From there, press the "Permissions" button and click the "+" button and add "Minecraft Chat Bridge" to the room and close the permissions window.

    Now that permissions are set, all you need to do is to do is to type "mc!chathere" and the bridge will take over if it is running.

Creating a discord bot and getting its discord token

    Head over to the discord developer portal and sign in if you haven't already.

    Press "Create an application". Once you do so, you may give the application a name and an icon if desired.

    From the left menu, click "Bot", then press "Add bot" on the next screen and "Yes do it" on the popup that appears.

    From here, you can once again give the Bot account your desired username and profile picture.

    Finally, Press the "Copy" button under the "Token" section to get your discord bot token. You can now paste it into this bridge's config.json file.

Creating the wildcard DNS record

This step can be achieved in a variety of ways but I will cover basic steps for setting this up using the cloudflare DNS servers.

    Create an account and add your domain to cloudflare

    From the cloudflare dashboard, click on your site and then click on the blue "DNS" icon at the top of the dashboard.

    From the DNS records section, select "A" in the dropdown, type any name you want in the "name" field and type in your computer's public IPv4 address into the "IPv4 address" field, then click on the orange cloud icon until it becomes gray and press the "Add Record" button. Note: If your computer is behind a NAT gateway, you will need to "port forward" the port specified in the AUTH_SERVER.PORT inside config.json to your computer's private IPv4 address.

    Repeat this process but choose "CNAME" from the dropdown, then type *.authentication (or anything you want as long as it starts with *.) and type whatever name you chose in step 3 into the "Domain name" box. Click on the orange cloud icon until it becomes gray and press the "Add Record" button.

    Set the AUTH_SERVER.DNS_WILDCARD node in config.json to the name you chose in step 4 (but without the *. part!!!) and append .yourdomain.tld to it where yourdomain.tld is the name of your domain. e.g. For the record *.authentication on domain example.com, you would write authentication.example.com.

Configuration

All of the configuration data is stored in a file called config.json that must be stored in the same directory as the executable
Json keys
Key 	Default value 	Explanation
MAIN.MC_USERNAME 	"" 	The username or e-mail address of the bot's minecraft account
MAIN.MC_PASSWORD 	"" 	The password of the bot's minecraft account
MAIN.MC_SERVER 	"" 	The IPv4 address of the minecraft server to connect to
MAIN.MC_PORT 	25565 	The port of the minecraft server to connect to
MAIN.MC_ONLINE 	true 	Whether or not to authenticate the bot's minecraft account with Mojang's authentications server
MAIN.DISCORD_APP_TOKEN 	"" 	The discord bot token the birdge will use to log into discord
MAIN.LOG_LEVEL 	"INFO" 	Set the log level, can be INFO or DEBUG
MAIN.MESSAGE_DELAY 	1.5 	Set the delay between messages sent from discord to minecraft
MAIN.ADMINS 	[283983554051047425] 	Array of discord user ids that have administrative access to the bot
AUTH_SERVER.BIND.IP 	"" 	The IPv4 address which the authentication server will bind to (set to blank for 0.0.0.0)
AUTH_SERVER.PORT 	9822 	The port which the authentication server will bind to
AUTH_SERVER.DNS_WILDCARD 	"" 	Must be set to a wildcard DNS CNAME record that points to an A record pointing to the authentication server's IP address
DATABASE.CONNECTION_STRING 	"sqlite:////data/db.sqlite" 	Must be set to any valid SQLAlchemy connection string. Defaults to an empty sqlit database in /data for docker user
ELASTICSEARCH.ENABLED 	false 	Whether or not to enable elasticsearch analytics collection
ELASTICSEARCH.URL 	"" 	Fully qualified URL to the elasticsearch server
ELASTICSEARCH.AUTH 	false 	Whether or not the elasticsearch http endpoint is protected by HTTP Basic authentication
ELASTICSEARCH.USERNAME 	"" 	HTTP Basic authentication username for elasticsearch
ELASTICSEARCH.PASSWORD 	"" 	HTTP Basic authentication password for elasticsearch
