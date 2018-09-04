DOMAIN = 'aligenie'

import asyncio
import json
import logging

from aiohttp import web
import async_timeout

from homeassistant.bootstrap import DATA_LOGGING
from homeassistant.components.http import HomeAssistantView
from homeassistant.const import (
    EVENT_HOMEASSISTANT_STOP, EVENT_TIME_CHANGED, HTTP_BAD_REQUEST,
    HTTP_CREATED, HTTP_NOT_FOUND, MATCH_ALL, URL_API, URL_API_COMPONENTS,
    URL_API_CONFIG, URL_API_DISCOVERY_INFO, URL_API_ERROR_LOG, URL_API_EVENTS,
    URL_API_SERVICES, URL_API_STATES, URL_API_STATES_ENTITY, URL_API_STREAM,
    URL_API_TEMPLATE, __version__)
import homeassistant.core as ha
from homeassistant.exceptions import TemplateError
from homeassistant.helpers import template
from homeassistant.helpers.service import async_get_all_descriptions
from homeassistant.helpers.state import AsyncTrackStates
from homeassistant.helpers.json import JSONEncoder
from homeassistant.core import State

_LOGGER = logging.getLogger(__name__)
places = []

def setup(hass, config):
    import subprocess
    # hass.states.set('hello.world', 'Paulus')

    hass.http.register_view(AliGenieGateView)
    
    command = 'curl https://open.bot.tmall.com/oauth/api/placelist'
    p = subprocess.Popen(command, shell=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    result, err = p.communicate()
    places = json.loads(result)['data']
    
    # aliases = json.loads(urlopen('https://open.bot.tmall.com/oauth/api/aliaslist').read().decode('utf-8'))['data']
    # aliases.append({'key': '电视', 'value': ['电视机']})
    return True



class AliGenieGateView(HomeAssistantView):
    """View to handle Configuration requests."""

    url = '/ali_genie_gate'
    name = 'ali_genie_gate'
    requires_auth = False

    async def post(self, request):
        """Update state of entity."""
        hass = request.app['hass']
        try:  
            data = await request.json()            
            response = await handleRequest(request, data)
        except ValueError:
            return self.json_message(
                "Invalid JSON specified.", HTTP_BAD_REQUEST)
        return self.json(response)


def errorResult(errorCode, messsage=None):
    messages = {
        'INVALIDATE_CONTROL_ORDER':    'invalidate control order',
        'SERVICE_ERROR': 'service error',
        'DEVICE_NOT_SUPPORT_FUNCTION': 'device not support',
        'INVALIDATE_PARAMS': 'invalidate params',
        'DEVICE_IS_NOT_EXIST': 'device is not exist',
        'IOT_DEVICE_OFFLINE': 'device is offline',
        'ACCESS_TOKEN_INVALIDATE': ' access_token is invalidate'
    }
    return {'errorCode': errorCode, 'message': messsage if messsage else messages[errorCode]}


#
async def handleRequest(request, data):
    header = data['header']
    payload = data['payload']
    properties = None
    name = header['name']

    # copied from async_validate_auth_header      
    hass = request.app['hass']  
    refresh_token = await hass.auth.async_validate_access_token(payload['accessToken'])
    token_valid = False
    if refresh_token is not None:
        token_valid = True
        request['hass_user'] = refresh_token.user

        namespace = header['namespace']
        if namespace == 'AliGenie.Iot.Device.Discovery':
            result = discoveryDevice(request)
        elif namespace == 'AliGenie.Iot.Device.Control':
            result = await controlDevice(request, name, payload)
        elif namespace == 'AliGenie.Iot.Device.Query':
            result = queryDevice(request, name, payload)
            if not 'errorCode' in result:
                properties = result
                result = {}
        else:
            result = errorResult('SERVICE_ERROR')
    else:
        result = errorResult('ACCESS_TOKEN_INVALIDATE')

    # Check error and fill response name
    header['name'] = ('Error' if 'errorCode' in result else name) + 'Response'

    # Fill response deviceId
    if 'deviceId' in payload:
        result['deviceId'] = payload['deviceId']

    response = {'header': header, 'payload': result}
    if properties:
        response['properties'] = properties
    return response

#
def discoveryDevice(request):
    items = request.app['hass'].states.async_all()
    # if _checkAlias:
    #     aliases = json.loads(urlopen('https://open.bot.tmall.com/oauth/api/aliaslist').read().decode('utf-8'))['data']
    #     aliases.append({'key': '电视', 'value': ['电视机']})
    # else:
    #     aliases = None
    #     log('Ignore alias checking to speed up!')
    aliases = None
    groups_ttributes = groupsAttributes(items)

    devices = []
    for item in items:
        attributes = item.attributes

        if attributes.get('hidden'):
            continue

        friendly_name = attributes.get('friendly_name')
        if friendly_name is None:
            continue

        entity_id = item.entity_id
        deviceType = guessDeviceType(entity_id, attributes)
        if deviceType is None:
            continue

        deviceName = guessDeviceName(entity_id, attributes, places, aliases)
        if deviceName is None:
            continue

        zone = guessZone(entity_id, attributes, places, groups_ttributes)
        if zone is None:
            continue

        prop,action = guessPropertyAndAction(entity_id, attributes, item.state)
        if prop is None:
            continue

        # Merge all sensors into one for a zone
        # https://bbs.hassbian.com/thread-2982-1-1.html
        if deviceType == 'sensor':
            for sensor in devices:
                if sensor['deviceType'] == 'sensor' and zone == sensor['zone']:
                    deviceType = None
                    if not action in sensor['actions']:
                        sensor['properties'].append(prop)
                        sensor['actions'].append(action)
                        sensor['model'] += ' ' + friendly_name
                        # SHIT, length limition in deviceId: sensor['deviceId'] += '_' + entity_id
                    else:
                        log('SKIP: ' + entity_id)
                    break
            if deviceType is None:
                continue
            deviceName = '传感器'
            entity_id = zone

        devices.append({
            'deviceId': entity_id,
            'deviceName': deviceName,
            'deviceType': deviceType,
            'zone': zone,
            'model': friendly_name,
            'brand': 'HomeAssistant',
            'icon': 'https://home-assistant.io/demo/favicon-192x192.png',
            'properties': [prop],
            'actions': ['TurnOn', 'TurnOff', 'Query', action] if action == 'QueryPowerState' else ['Query', action],
            'extensions':{'extension1':'','extension2':''}
            })
    return {'devices': devices}

#
async def controlDevice(request, name, payload):
    entity_id = payload['deviceId']
    service = getControlService(name)
    domain = entity_id[:entity_id.find('.')]
    data = {"entity_id": entity_id }
    if domain == 'cover':
        service = 'close_cover' if service == 'turn_off' else 'open_cover'
        
    hass = request.app['hass']
    with AsyncTrackStates(hass) as changed_states:
        result = await hass.services.async_call(domain, service, data, True)
    return {} if result else errorResult('IOT_DEVICE_OFFLINE')

#
def queryDevice(request, name, payload):
    deviceId = payload['deviceId']

    if payload['deviceType'] == 'sensor':
        items = request.app['hass'].states.get(deviceId)

        entity_ids = None
        for item in items:
            attributes = item.attributes
            if item.entity_id.startswith('group.') and (attributes['friendly_name'] == deviceId or attributes.get('hagenie_zone') == deviceId):
                entity_ids = attributes.get('entity_id')
                break

        if entity_ids:
            properties = [{'name':'powerstate', 'value':'on'}]
            for item in items:
                entity_id = item.entity_id
                attributes = item.attributes
                if entity_id.startswith('sensor.') and (entity_id in entity_ids or attributes['friendly_name'].startswith(deviceId) or attributes.get('hagenie_zone') == deviceId):
                    prop,action = guessPropertyAndAction(entity_id, attributes, item.state)
                    if prop is None:
                        continue
                    properties.append(prop)
            return properties
    else:
        item = request.app['hass'].states.get(deviceId)
        if isinstance(item, State):
            return {'name':'powerstate', 'value':item.state}
    return errorResult('IOT_DEVICE_OFFLINE')


#
def groupsAttributes(items):
    groups_attributes = []
    for item in items:
        group_entity_id = item.entity_id
        if group_entity_id.startswith('group.') and not group_entity_id.startswith('group.all_') and group_entity_id != 'group.default_view':
            group_attributes = item.attributes
            if 'entity_id' in group_attributes:
                groups_attributes.append(group_attributes)
    return groups_attributes


#
def getControlService(action):
    i = 0
    service = ''
    for c in action:
        service += (('_' if i else '') + c.lower()) if c.isupper() else c
        i += 1
    return service
        
DEVICE_TYPES = [
    'television',#: '电视',
    'light',#: '灯',
    'aircondition',#: '空调',
    'airpurifier',#: '空气净化器',
    'outlet',#: '插座',
    'switch',#: '开关',
    'roboticvacuum',#: '扫地机器人',
    'curtain',#: '窗帘',
    'humidifier',#: '加湿器',
    'fan',#: '风扇',
    'bottlewarmer',#: '暖奶器',
    'soymilkmaker',#: '豆浆机',
    'kettle',#: '电热水壶',
    'watercooler',#: '饮水机',
    'cooker',#: '电饭煲',
    'waterheater',#: '热水器',
    'oven',#: '烤箱',
    'waterpurifier',#: '净水器',
    'fridge',#: '冰箱',
    'STB',#: '机顶盒',
    'sensor',#: '传感器',
    'washmachine',#: '洗衣机',
    'smartbed',#: '智能床',
    'aromamachine',#: '香薰机',
    'window',#: '窗',
    'kitchenventilator',#: '抽油烟机',
    'fingerprintlock',#: '指纹锁'
    'telecontroller',#: '万能遥控器'
    'dishwasher',#: '洗碗机'
    'dehumidifier',#: '除湿机'
]

INCLUDE_DOMAINS = {
    'climate': 'aircondition',
    'fan': 'fan',
    'light': 'light',
    'media_player': 'television',
    'remote': 'telecontroller',
    'switch': 'switch',
    'vacuum': 'roboticvacuum',
    }

EXCLUDE_DOMAINS = [
    'automation',
    'binary_sensor',
    'device_tracker',
    'group',
    'zone',
    ]

# http://doc-bot.tmall.com/docs/doc.htm?treeId=393&articleId=108271&docType=1
def guessDeviceType(entity_id, attributes):
    if 'hagenie_deviceType' in attributes:
        return attributes['hagenie_deviceType']

    # Exclude with domain
    domain = entity_id[:entity_id.find('.')]
    if domain in EXCLUDE_DOMAINS:
        return None

    # Guess from entity_id
    for deviceType in DEVICE_TYPES:
        if deviceType in entity_id:
            return deviceType

    # Map from domain
    return INCLUDE_DOMAINS[domain] if domain in INCLUDE_DOMAINS else None


# https://open.bot.tmall.com/oauth/api/aliaslist
def guessDeviceName(entity_id, attributes, places, aliases):
    if 'hagenie_deviceName' in attributes:
        return attributes['hagenie_deviceName']

    # Remove place prefix
    name = attributes['friendly_name']
    for place in places:
        if name.startswith(place):
            name = name[len(place):]
            break

    if aliases is None or entity_id.startswith('sensor'):
        return name


    # Name validation
    for alias in aliases:
        if name == alias['key'] or name in alias['value']:
            return name

    return None


#
def groupsAttributes(items):
    groups_attributes = []
    for item in items:
        group_entity_id = item.entity_id
        if group_entity_id.startswith('group.') and not group_entity_id.startswith('group.all_') and group_entity_id != 'group.default_view':
            group_attributes = item.attributes
            if 'entity_id' in group_attributes:
                groups_attributes.append(group_attributes)
    return groups_attributes


# https://open.bot.tmall.com/oauth/api/placelist
def guessZone(entity_id, attributes, places, groups_attributes):
    if 'hagenie_zone' in attributes:
        return attributes['hagenie_zone']

    # Guess with friendly_name prefix
    name = attributes['friendly_name']
    for place in places:
        if name.startswith(place):
            return place

    # Guess from HomeAssistant group
    for group_attributes in groups_attributes:
        for child_entity_id in group_attributes['entity_id']:
            if child_entity_id == entity_id:
                if 'hagenie_zone' in group_attributes:
                    return group_attributes['hagenie_zone']
                return group_attributes['friendly_name']

    return None

#
def guessPropertyAndAction(entity_id, attributes, state):
    # http://doc-bot.tmall.com/docs/doc.htm?treeId=393&articleId=108264&docType=1
    # http://doc-bot.tmall.com/docs/doc.htm?treeId=393&articleId=108268&docType=1
    # Support On/Off/Query only at this time
    if 'hagenie_propertyName' in attributes:
        name = attributes['hagenie_propertyName']

    elif entity_id.startswith('sensor.'):
        unit = attributes['unit_of_measurement'] if 'unit_of_measurement' in attributes else ''
        if unit == u'°C' or unit == u'℃':
            name = 'Temperature'
        elif unit == 'lx' or unit == 'lm':
            name = 'Brightness'
        elif ('hcho' in entity_id):
            name = 'Fog'
        elif ('humidity' in entity_id):
            name = 'Humidity'
        elif ('pm25' in entity_id):
            name = 'PM2.5'
        elif ('co2' in entity_id):
            name = 'WindSpeed'
        else:
            return (None, None)
    else:
        name = 'PowerState'
        if state != 'off':
            state = 'on'
    return ({'name': name.lower(), 'value': state}, 'Query' + name)
