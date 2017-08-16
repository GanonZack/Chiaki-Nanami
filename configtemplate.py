# For future cogs, this module should never be imported. Instead, the bot will
# have a series of properties that are merely thin wrappers around this module

# ----------------------- STUFF ---------------------

# The bot's token, this is NOT to be confused with the "Client Secret"
# KEEP THIS PRIVATE AT ALL COSTS
token = ''

# The API key for Carbonitex. Also keep this private.
carbon_key = ''

# The API key for Discord Bots. Again, keep this private.
bots_key = ''

# -------------------- BOT STUFF ---------------------

# The bot's default command prefix. This can either be a string, 
# or a tuple/list of prefixes
command_prefix = '->'

# The bot's description
description = "I'm Chiaki Nanami, the gamer for gamers!"

# The extensions that will be initially loaded when the bot starts
extensions = []

# The possible games the bot will randomly choose from for the playing status.
# These are not cycled.
games = []

# ----------------------- COLOURS ---------------------

# The default colour the bot will use for embeds
# It must be an integer
# You can use hexadecimal literals for extra readability (eg 0xFFFFFF)
colour = 0

# The colour used for the ok embeds. This colour is used on embeds when 
# something went ok
ok_colour = 0x00FF00

# The colour used for the error embeds. This colour is used on embeds when 
# something went wrong
error_colour = 0xFF000

