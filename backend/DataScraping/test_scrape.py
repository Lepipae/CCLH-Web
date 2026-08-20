import requests
from bs4 import BeautifulSoup

headers = {'User-Agent': 'Mozilla/5.0'}
url = "https://html.duckduckgo.com/html/?q=cards+against+humanity+list+of+cards"
response = requests.get(url, headers=headers)

if response.status_code == 200:
    soup = BeautifulSoup(response.text, 'html.parser')
    for a in soup.find_all('a', class_='result__url'):
        print("Found link:", a.get('href'))
else:
    print("Failed", response.status_code)
