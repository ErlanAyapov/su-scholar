from main.models import Publication, PublicationFile, PublicationFile
import requests
import time
import requests
from django.core.files.base import ContentFile
import urllib3


def collect_doi_files():
    publications = Publication.objects.filter(doi__isnull=False, doi__gt='')
    total = publications.count()
    
    for i, publication in enumerate(publications, 1):
        print(f'[{i}/{total}] {publication.doi}')
        
        try:
            url = f'https://api.crossref.org/works/{publication.doi}'
            headers = {'User-Agent': 'MyApp/1.0 (mailto:your@email.com)'}
            
            response = requests.get(url, headers=headers, timeout=10)
            
            if response.status_code == 429:
                retry_after = int(response.headers.get('Retry-After', 10))
                print(f'  Rate limited, waiting {retry_after}s...')
                time.sleep(retry_after)
                response = requests.get(url, headers=headers, timeout=10)
            
            response.raise_for_status()
            data = response.json()
            
            links = data['message'].get('link', [])
            pdf_url = next((l['URL'] for l in links if l['content-type'] == 'application/pdf'), None)
            
            if not pdf_url:
                print(f'  No PDF')
                time.sleep(1)
                continue
            if PublicationFile.objects.filter(source_url=pdf_url).exists():
                print(f'  Already downloaded, skipping')
                continue
            urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
            pdf_response = requests.get(pdf_url, headers=headers, timeout=30, verify=False)
            pdf_response.raise_for_status()
            
            if 'application/pdf' not in pdf_response.headers.get('Content-Type', ''):
                print(f'  Not a PDF')
                time.sleep(1)
                continue
            
            filename = f'{publication.doi.replace("/", "_")}.pdf'
            PublicationFile.objects.create(
                publication=publication,
                source_url=pdf_url,
                file=ContentFile(pdf_response.content, name=filename)
            )
            print(f'  Saved: {filename}')

        except Exception as e:
            print(f'  Error: {e}')
        
        time.sleep(1)  # пауза между каждым запросом