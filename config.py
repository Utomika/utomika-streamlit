import os


# Resolve data files relative to this script, not the process CWD — so the app
# works regardless of where Streamlit Cloud runs it from.
_HERE = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(_HERE, "data")

# Scenes are shown in pages of this size in the sidebar radio, rather than one
# flat list of every filtered row (thousands of options was the main render cost).
PAGE_SIZE = 25

CITIES = {"Zagreb": "zagreb", "Split": "split"}
LABELS = ("clear", "cloudy", "unsure")
LABEL_EMOJI = {"clear": "☀️ clear", "cloudy": "☁️ cloudy", "unsure": "❓ unsure"}
