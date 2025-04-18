import requests

class Torbox:
    def __init__(self, apikey):
        self.apikey = apikey
    
    def send_request(self, method: str, endpoint: str, params: dict = None, files: dict = None, data: dict = None) -> dict:
        url = "https://api.torbox.app/v1/api/" + endpoint
        headers = {"Authorization": f"Bearer {self.apikey}"}
        if method == "GET":
            response = requests.get(url, params=params, files=files, headers=headers, data=data)
            return response.json()
        elif method == "POST":
            response = requests.post(url, params=params, files=files, headers=headers, data=data)
            return response.json()
    
    def create_usenet_download(self, file_bytes: bytes, filename: str) -> int:
        files = {'file': (filename, file_bytes, 'application/x-nzb')}
        data = {'name': filename}
        response = self.send_request("POST", "usenet/createusenetdownload", params={"name": filename}, files=files, data=data)
        if response.get("success") != True:
            raise Exception(f"Error creating usenet download: {response}")
        return response.get("data").get("usenetdownload_id")
    
    def is_usenet_download_ready(self, usenetdownload_id: int) -> bool:
        return not (self.get_usenet_download_file_list(usenetdownload_id) == [])
    
    def get_usenet_download_file_list(self, usenetdownload_id: int) -> list:
        response = self.send_request("GET", "usenet/mylist", params={"id": usenetdownload_id})
        return response.get("data").get("files")
    
    def get_usenet_file_download_link(self, usenetdownload_id: int, file_id: int) -> str:
        response = self.send_request("GET", "usenet/requestdl", params={"usenet_id": usenetdownload_id, "file_id": file_id, "token": self.apikey})
        if response.get("success") != True:
            raise Exception(f"Error getting download link: {response}")
        return response.get("data")