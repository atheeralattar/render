# Add these imports and configuration at the top of your app.py
from selenium.webdriver.chrome.options import Options
import os
import shutil

# Configure Chrome options for Docker environment
def get_chrome_options():
    options = Options()
    options.add_argument("--headless")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--disable-gpu")
    options.add_argument("--window-size=1920,1080")
    options.add_argument("--disable-extensions")
    
    # Set Chrome binary location explicitly
    options.binary_location = "/usr/bin/google-chrome"
    
    # Chrome preferences for downloads
    prefs = {
        "download.default_directory": "/app/downloads",
        "download.prompt_for_download": False,
        "download.directory_upgrade": True,
        "plugins.always_open_pdf_externally": True
    }
    options.add_experimental_option("prefs", prefs)
    
    return options

# Then replace your Chrome options setup in your download function:
options = get_chrome_options()