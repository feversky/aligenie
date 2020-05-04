import json
import logging

import voluptuous as vol
from homeassistant.helpers import config_validation as cv

from homeassistant.components.http import HomeAssistantView
from homeassistant.const import (MAJOR_VERSION, MINOR_VERSION)
from homeassistant.auth.const import ACCESS_TOKEN_EXPIRATION
import homeassistant.auth.models as models
from typing import Optional
from datetime import timedelta
from homeassistant.helpers.state import AsyncTrackStates
from urllib.request import urlopen
_LOGGER = logging.getLogger(__name__)

MAIN = 'aligenie'

EXPIRE_HOURS = 'expire_hours'
DOMAIN       = 'aligenie'

CONFIG_SCHEMA = vol.Schema({
    DOMAIN: vol.Schema({
        vol.Optional(EXPIRE_HOURS): cv.positive_int
    })
}, extra=vol.ALLOW_EXTRA)

_hass         = None
_expire_hours = None
_places       = []
_aliases      = []

async def async_create_refresh_token77(
        user: models.User, client_id: Optional[str] = None) \
        -> models.RefreshToken:
    """Create a new token for a user."""
    _LOGGER.info('access token expiration: %d hours', _expire_hours)
    refresh_token = models.RefreshToken(user=user, 
                                        client_id=client_id,
                                        access_token_expiration = timedelta(hours=_expire_hours))
    user.refresh_tokens[refresh_token.id] = refresh_token
    _hass.auth._store._async_schedule_save()
    return refresh_token

async def async_create_refresh_token78(
        user: models.User, client_id: Optional[str] = None,
        client_name: Optional[str] = None,
        client_icon: Optional[str] = None,
        token_type: str = models.TOKEN_TYPE_NORMAL,
        access_token_expiration: timedelta = ACCESS_TOKEN_EXPIRATION) \
        -> models.RefreshToken:
    if access_token_expiration == ACCESS_TOKEN_EXPIRATION:
        access_token_expiration = timedelta(hours=_expire_hours)
    _LOGGER.info('Access token expiration: %d hours', _expire_hours)
    """Create a new token for a user."""
    kwargs = {
        'user': user,
        'client_id': client_id,
        'token_type': token_type,
        'access_token_expiration': access_token_expiration
    }  # type: Dict[str, Any]
    if client_name:
        kwargs['client_name'] = client_name
    if client_icon:
        kwargs['client_icon'] = client_icon

    refresh_token = models.RefreshToken(**kwargs)
    user.refresh_tokens[refresh_token.id] = refresh_token

    _hass.auth._store._async_schedule_save()
    return refresh_token

async def async_setup(hass, config):
    global _hass, _expire_hours
    _hass         = hass
    _expire_hours = config[DOMAIN].get(EXPIRE_HOURS)
    
    if _expire_hours is not None:
        if MAJOR_VERSION == 0 and MINOR_VERSION <= 77:
            _hass.auth._store.async_create_refresh_token = async_create_refresh_token77
        else:
            _hass.auth._store.async_create_refresh_token = async_create_refresh_token78
    _hass.http.register_view(AliGenieGateView)

    global _places, _aliases
    _places  = json.loads(urlopen('https://open.bot.tmall.com/oauth/api/placelist').read().decode('utf-8'))['data']
    _aliases = json.loads(urlopen('https://open.bot.tmall.com/oauth/api/aliaslist').read().decode('utf-8'))['data']
    _aliases.append({'key': '电视', 'value': ['电视机']})
    return True

class AliGenieGateView(HomeAssistantView):
    """View to handle Configuration requests."""

    url = '/ali_genie_gate'
    name = 'ali_genie_gate'
    requires_auth = False

    async def post(self, request):
        """Update state of entity."""
        try:
            data = await request.json()
            response = await handleRequest(data)
        except:
            import traceback
            _LOGGER.error(traceback.format_exc())
            response = {'header': {'name': 'errorResult'}, 'payload': errorResult('SERVICE_ERROR', 'service exception')}

        return self.json(response)

def errorResult(errorCode, messsage=None):
    """Generate error result"""
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

async def handleRequest(data):
    """Handle request"""
    header = data['header']
    payload = data['payload']
    properties = None
    name = header['name']
    _LOGGER.info("Handle Request: %s", data)

    token = await _hass.auth.async_validate_access_token(payload['accessToken'])
    if token is not None:
        namespace = header['namespace']
        if namespace == 'AliGenie.Iot.Device.Discovery':
            result = discoveryDevice()
        elif namespace == 'AliGenie.Iot.Device.Control':
            result = await controlDevice(name, payload)
        elif namespace == 'AliGenie.Iot.Device.Query':
            result = queryDevice(name, payload)
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
    _LOGGER.info("Respnose: %s", response)
    return response

def discoveryDevice():

    states = _hass.states.async_all()
    groups_ttributes = groupsAttributes(states)

    devices = []
    for state in states:
        attributes = state.attributes

        if attributes.get('hidden') or attributes.get('hagenie_hidden'):
            continue

        friendly_name = attributes.get('friendly_name')
        if friendly_name is None:
            continue

        entity_id = state.entity_id
        deviceType = guessDeviceType(entity_id, attributes)
        if deviceType is None:
            continue

        deviceName = guessDeviceName(entity_id, attributes, _places, _aliases)
        if deviceName is None:
            continue

        zone = guessZone(entity_id, attributes, groups_ttributes, _places)
        if zone is None:
            continue

        prop,action = guessPropertyAndAction(entity_id, attributes, state.state)
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
                        _LOGGER.info('SKIP: ' + entity_id)
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
            'icon': 'https://home-assistant.io/images/favicon-192x192.png',
            'properties': [prop],
            'actions': ALL_ACTIONS + ['Query'] if action == 'QueryPowerState' else ['Query', action],
            #'actions': ['TurnOn', 'TurnOff', 'Query', action] if action == 'QueryPowerState' else ['Query', action],
            #'extensions':{'extension1':'','extension2':''}
            })

    #for sensor in devices:
        #if sensor['deviceType'] == 'sensor':
            #_LOGGER.info(json.dumps(sensor, indent=2, ensure_ascii=False))
    return {'devices': devices}

async def controlDevice(action, payload):
    entity_id = payload['deviceId']
    domain = entity_id[:entity_id.find('.')]
    data = {"entity_id": entity_id }
    if domain in TRANSLATIONS.keys():
        translation = TRANSLATIONS[domain][action]
        if callable(translation):
            service, content = translation(_hass.states.get(entity_id), payload)
            data.update(content)
        else:
            service = translation
    else:
        service = getControlService(action)

    with AsyncTrackStates(_hass) as changed_states:
        result = await _hass.services.async_call(domain, service, data, True)

    return {} if result else errorResult('IOT_DEVICE_OFFLINE')

def queryDevice(name, payload):
    deviceId = payload['deviceId']

    if payload['deviceType'] == 'sensor':

        states = _hass.states.async_all()

        entity_ids = []
        for state in states:
            attributes = state.attributes
            if state.entity_id.startswith('group.') and (attributes['friendly_name'] == deviceId or attributes.get('hagenie_zone') == deviceId):
                entity_ids = attributes.get('entity_id')
                break

        properties = [{'name':'powerstate', 'value':'on'}]
        for state in states:
            entity_id = state.entity_id
            attributes = state.attributes
            if entity_id.startswith('sensor.') and (entity_id in entity_ids or attributes['friendly_name'].startswith(deviceId) or attributes.get('hagenie_zone') == deviceId):
                prop,action = guessPropertyAndAction(entity_id, attributes, state.state)
                if prop is None:
                    continue
                properties.append(prop)
        return properties
    else:
        state = _hass.states.get(deviceId)
        if state is not None or state.state != 'unavailable':
            return {'name':'powerstate', 'value':'off' if state.state == 'off' else 'on'}
    return errorResult('IOT_DEVICE_OFFLINE')

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

ALL_ACTIONS = [
    'TurnOn',
    'TurnOff',
    'SelectChannel',
    'AdjustUpChannel',
    'AdjustDownChannel',
    'AdjustUpVolume',
    'AdjustDownVolume',
    'SetVolume',
    'SetMute',
    'CancelMute',
    'Play',
    'Pause',
    'Continue',
    'Next',
    'Previous',
    'SetBrightness',
    'AdjustUpBrightness',
    'AdjustDownBrightness',
    'SetTemperature',
    'AdjustUpTemperature',
    'AdjustDownTemperature',
    'SetWindSpeed',
    'AdjustUpWindSpeed',
    'AdjustDownWindSpeed',
    'SetMode',
    'SetColor',
    'OpenFunction',
    'CloseFunction',
    'Cancel',
    'CancelMode']

mapping = lambda dict, key: dict[key] if key in dict else key

TRANSLATIONS = {
    'cover': {
        'TurnOn':  'open_cover',
        'TurnOff': 'close_cover'
    },
    'vacuum': {
        'TurnOn':  'start',
        'TurnOff': 'return_to_base'
    },
    'light': {
        'TurnOn':  'turn_on',
        'TurnOff': 'turn_off',
        'SetBrightness':        lambda state, payload: ('turn_on', {'brightness_pct': mapping({'max': 100, 'min': 1}, payload['value'])}),
        'AdjustUpBrightness':   lambda state, payload: ('turn_on', {'brightness_pct': min(state.attributes['brightness'] * 100 // 255 + int(payload['value']), 100)}),
        'AdjustDownBrightness': lambda state, payload: ('turn_on', {'brightness_pct': max(state.attributes['brightness'] * 100 // 255 - int(payload['value']), 0)}),
        'SetColor':             lambda state, payload: ('turn_on', {"color_name": payload['value']})
    },
    'climate': {
        'TurnOn': 'turn_on',
        'TurnOff': 'turn_off',
        'SetTemperature': lambda state, payload: ('set_temperature', {'temperature': int(payload['value'])}),
        'AdjustUpTemperature': lambda state, payload: ('set_temperature', {'temperature': min(state.attributes['temperature'] + int(payload['value']), state.attributes['max_temp'])}),
        'AdjustDownTemperature': lambda state, payload: ('set_temperature', {'temperature': max(state.attributes['temperature'] - int(payload['value']), state.attributes['min_temp'])}),
        'SetMode': lambda state, payload: ('set_operation_mode', {'operation_mode': mapping({'cold': 'cool'}, payload['value'])}),
        'SetWindSpeed': lambda state, payload: ('set_fan_mode', {'fan_mode': mapping({'max': 'high', 'min': 'low'}, payload['value'])}),
    },
    'fan': {
        'TurnOn': 'turn_on',
        'TurnOff': 'turn_off',
        'SetWindSpeed': lambda state, payload: ('set_speed', {'speed':mapping({'max': 'high', 'min': 'low'}, payload['value'])}),
        'OpenSwing': lambda state, payload: ('oscillate', {'oscillating': True}),
        'CloseSwing': lambda state, payload: ('oscillate', {'oscillating': False}),
    }
}

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

    _LOGGER.error('%s is not a valid name in https://open.bot.tmall.com/oauth/api/aliaslist', name)
    return None

def groupsAttributes(states):
    groups_attributes = []
    for state in states:
        group_entity_id = state.entity_id
        if group_entity_id.startswith('group.') and not group_entity_id.startswith('group.all_') and group_entity_id != 'group.default_view':
            group_attributes = state.attributes
            if 'entity_id' in group_attributes:
                groups_attributes.append(group_attributes)
    return groups_attributes

# https://open.bot.tmall.com/oauth/api/placelist
def guessZone(entity_id, attributes, groups_attributes, places):
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
