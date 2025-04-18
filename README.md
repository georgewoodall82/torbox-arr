# torbox-arr
Proof of concept that allows torbox to be used as a usenet downloader in the arrs (currently only tested sonarr and lidarr)

I am aware the code for this is a mess, so if anybody wants to fix anything with a pull request that would be greatly appreciated

## Installation
1. Download this repository, and move the contents of it, which includes the json and py files to some random folder
2. Install python 3, and these packages with pip or your system package manager: `pip install uvicorn aiofiles httpx fastapi`
3. Edit config.json to contain your torbox access token, and your download directory. Fake directory might be the same as your normal one depending on if you are using docker or not.
5. Run main.py with python3 `python3 main.py` or if you don't want it to quit when you close your ssh client: `nohup python3 main.py &`
6. Configure sabnzbd as a download client in whatevarr with the correct ip and the port `8000`. the api key can be anything.
