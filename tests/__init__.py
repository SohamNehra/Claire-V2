import os

# Tests never read the model chosen in the app on this machine (models.json in
# the real state folder); the model tests choose one in a temporary folder.
os.environ["CLAIRE_MODELS"] = "off"
