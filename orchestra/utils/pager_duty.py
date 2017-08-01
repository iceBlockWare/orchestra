import requests
import json

class PagerDuty(object):
    def __init__(self, api_key='', service_integration_key=''):
        self.api_key = api_key
        self.service_int egration_key = service_integration_key

    def send_alert(self, description, client):
        url = 'https://events.pagerduty.com/generic/2010-04-15/create_event.json'
        data = {    
            'service_key': self.service_integration_key,
            'event_type': 'trigger',
            'description': description,
            'client': client,
        }
        try:
            response = requests.post(url, data=json.dumps(data))
        except: 
            return False

        if response.status_code == 200:
            return json.loads(response.text)
        else:
            return False
