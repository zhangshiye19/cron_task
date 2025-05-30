# coding=utf-8
# encoding:utf-8
import sys
import datetime
import time
import base64
import hashlib
import hmac
import requests
import json
import schedule
import signal
import concurrent.futures
from sts_sdk.model.enums import AuthAction
from sts_sdk.model.subject import STSRequest, SignParam
from sts_sdk.service.signature_service_factory import STSServiceFactory

# ====================== 配置常量 ======================
# 公众号ID
PUB_ID = 138648568684  # <开发者信息中的PubID>
# 开发者信息的appKey
CLIENT_ID = 'g250140310265012'
# 开发者信息的appToken
CLIENT_SECRET = '9700dbf5f91025c77158ca02dd059869'
NEIXIN_HOST = 'https://xmapi.vip.sankuai.com'
# 向群组推送消息
PUSH_URL = '/api/pub/pushToRoom'
# 接收消息的群组ID

ONES_OPEN_API_TOKEN = '' # 动态获取

# 测试
TOGID = 69429561756

# 触评周会晨会群
# TOGID = 69429561756  

ONES_HOST = 'https://ones.sankuai.com'
WORKTIME_URL = '/api/1.0/ones/projects/load/user/worktime'
ISSUE_URL = '/api/1.0/ones/issue/{issueId}'
ASSOCIATE_URL = '/api/1.0/ones/issue/{issueId}/associate/search'
USERS = {}

# 缓存父需求信息
parent_requirement_cache = {}

# 时间戳常量
TIMESTAMP_2024_12_01 = 1732982400000  # 2024年12月1日的时间戳

# ====================== ONES API相关函数 ======================

def get_ones_open_api_token():
    """
    获取ONES API的访问令牌

    Returns:
        str: ONES API访问令牌
    """
    global ONES_OPEN_API_TOKEN
    if ONES_OPEN_API_TOKEN:
        return ONES_OPEN_API_TOKEN
    else:
        sts_request = STSRequest("com.sankuai.ones.openapi.auth", "OPENAPI-IAM", AuthAction.SIGN, online_req=True) 
        # 创建STSService
        sts_service = STSServiceFactory.create(sts_request)
        # 构建SignParam 底下clientId 替换为 实际的IAM账号，iampwd 替换为IAM账号的实际密码
        sign_param = SignParam(client_id="it_ones_remind", ext_map = {"iampwd" : "KHaqfa@286"})
        # 签发Token
        auth_token = sts_service.sign(sign_param)
        # print(f"获取STS Token成功: {auth_token.at}")
        return auth_token.at


def get_issues_id_by_uname(username):
    """
    获取用户关联的issue_id,包括创建、指派、技术主R、测试主R等关联

    Args:
        username: 用户名

    Returns:
        list: 用户关联的工作项列表
    """
    pageNumber = 1
    pageSize = 100
    result = []
    while pageSize == 100:
        headers = {
            "auth-type": "user-sts",
            "sts-token": get_ones_open_api_token()
        }
        # 发送请求
        response = requests.post(
            "http://ones.vip.sankuai.com/open/ones/v1/user/workitems/filter?pageNumber=" + str(pageNumber) + "&pageSize=" + str(pageSize),
            headers=headers,
            data = {
                "misId": username
            }
        ).json()
        pageSize = len(response['data']['data'])
        result.extend(response['data']['data'])
        pageNumber += 1
    return result


def get_issue_detail_parallel(issue_ids):
    """
    并行获取多个issue的详情

    Args:
        issue_ids: issue ID列表

    Returns:
        dict: {issue_id: response_data}
    """
    results = {}

    # 过滤掉None和已缓存的issue_id
    to_fetch = []
    for issue_id in issue_ids:
        if issue_id is None:
            continue
        if issue_id in parent_requirement_cache:
            results[issue_id] = parent_requirement_cache[issue_id]
        else:
            to_fetch.append(issue_id)

    if not to_fetch:
        return results

    def fetch_single_issue(issue_id):
        """获取单个issue的详情"""
        headers = {
            'Content-Type': 'application/json',
            'Authorization': 'Basic aXRfYmluZ3FpbGluOml0X2JpbmdxaWxpbl9vbmVz'
        }
        response = requests.get(ONES_HOST + ISSUE_URL.format(issueId=issue_id), headers=headers)
        return issue_id, response.json()

    # 使用线程池并行请求
    with concurrent.futures.ThreadPoolExecutor(max_workers=10) as executor:
        future_to_issue = {executor.submit(fetch_single_issue, issue_id): issue_id for issue_id in to_fetch}
        for future in concurrent.futures.as_completed(future_to_issue):
            issue_id, result = future.result()
            results[issue_id] = result
            # 更新缓存
            parent_requirement_cache[issue_id] = result

    return results


def get_associated_issues(issue_id, project_id, associate_type):
    """
    获取关联的任务ID列表

    Args:
        issue_id: 任务ID
        project_id: 项目ID
        associate_type: 关联类型

    Returns:
        list: 关联任务ID列表
    """
    url = f"{ONES_HOST}/api/1.0/ones/issue/{issue_id}/associate/search"
    params = {
        "projectId": project_id,
        "associateType": associate_type,
    }
    headers = {
        'Content-Type': 'application/json',
        'Authorization': 'Basic aXRfYmluZ3FpbGluOml0X2JpbmdxaWxpbl9vbmVz'
    }
    try:
        response = requests.get(url, params=params, headers=headers)
        if response.status_code == 200:
            data = response.json()
            if 'data' in data and 'items' in data['data']:
                items = data['data']['items']
                return [item['id']['value'] for item in items if 'id' in item and 'value' in item['id']]
            return []
    except Exception as e:
        print(f"请求关联任务接口异常: {str(e)}")
        return []
    return []

# ====================== 消息推送相关函数 ======================

def gen_headers(client_id, client_secret, url_path, http_method):
    """
    生成大象API请求所需的认证头

    Args:
        client_id: 客户端ID
        client_secret: 客户端密钥
        url_path: 请求路径
        http_method: HTTP方法

    Returns:
        dict: 认证头信息
    """
    timestamp = time.strftime('%a, %d %b %Y %H:%M:%S GMT', time.gmtime())
    string_to_sign = ('%s %s\n%s' % (http_method, url_path, timestamp))
    hmac_bytes = hmac.new(bytes(client_secret.encode('ascii')),
                          bytes(string_to_sign.encode('ascii')),
                          hashlib.sha1).digest()
    auth = base64.b64encode(hmac_bytes).decode("utf-8")
    return {
        'Date': timestamp,
        'Authorization': 'MWS %s:%s' % (client_id, auth),
        'Content-Type': 'application/json;charset=utf-8',
    }


def push_issue_to_room(room_id, text):
    """
    向指定群组发送消息

    Args:
        room_id: 群组ID
        text: 消息文本

    Returns:
        int: 请求状态码
    """
    headers = gen_headers(CLIENT_ID, CLIENT_SECRET, PUSH_URL, 'PUT')
    data = {
        'fromUid': PUB_ID,
        'toGid': room_id if room_id else TOGID,
        'messageType': 'text',
        'body': {
            'text': text
        }
    }
    r = requests.put(
        NEIXIN_HOST + PUSH_URL,
        headers=headers,
        data=json.dumps(data, indent=2, ensure_ascii=False).encode('utf-8')
    )
    print(f"消息长度: {len(text)}")
    return r.status_code

# ====================== 工作项字段检查相关函数 ======================

def check_task_fields(issue_data, state_value=None):
    """
    检查任务数据并返回缺失的字段信息
    
    Args:
        issue_data: 任务数据字典
        state_value: 任务状态值
        
    Returns:
        tuple: (是否完整, 缺失字段列表, 字段提示信息)
    """
    missing_fields = []

    # 检查任务类型
    if not issue_data.get('subtypeId') or issue_data.get('subtypeId') not in [191906, 191908]:
        missing_fields.append("任务类型")
        return len(missing_fields) == 0, missing_fields, {"任务类型": "请修改为前端/客户端交付单元"}
    
    # 技术设计开始时间始终检测
    if not issue_data.get('customField17425'):
        missing_fields.append('技术设计开始时间')
    
    # 只有在非规划中状态下才检测其他字段
    if state_value != '规划中':
        # 检查代码全量时间
        # 检查创建时间是否大于30天
        current_time = int(time.time() * 1000)  # 转换为毫秒
        thirty_days_ago = current_time - (30 * 24 * 60 * 60 * 1000)  # 30天前的时间戳
        # 如果是测试完成或者已上线状态，或者创建时间大于30天了才做检查
        # if state_value in ['测试完成','已上线'] or thirty_days_ago > issue_data['createdAt']: 
        if state_value in ['测试完成', '已上线']:
            if not issue_data.get('customField13031'): # 
                missing_fields.append('代码全量时间')
        # 检查提测时间
        if state_value in ['待测试', '测试中', '测试完成', '已上线']:
            if not issue_data.get('customField11553'):
                missing_fields.append('提测时间')
        # 检查开发开始时间
        if not issue_data.get('customField13024'):
            missing_fields.append('开发开始时间')
        # 预计工作量的判断逻辑 - 只要不大于0就提示未填写
        expect_time = issue_data.get('expectTime')
        if expect_time is None or expect_time <= 0:
            missing_fields.append('预计工作量')
        
    return len(missing_fields) == 0, missing_fields, {}


def check_requirement_fields(issue_data, issue_id):
    """
    检查需求数据并返回缺失的字段信息
    
    Args:
        issue_data: 需求数据字典
        issue_id: 需求ID
        
    Returns:
        tuple: (是否完整, 缺失字段列表)
    """
    missing_fields = []
    
    # if not issue_data.get('customField10243'):
    #     missing_fields.append('需求提出时间')
    # print(type(issue_id),issue_id,IGNORED_REQUIREMENT_ISFOLLOW,issue_id in IGNORED_REQUIREMENT_ISFOLLOW)
    if issue_data.get('customField25044') is None:
        missing_fields.append('是否跟版')
    # if not issue_data.get('customField2'):
    #     missing_fields.append('验证人')
    # if not issue_data.get('customField13024'):
    #     missing_fields.append('开发开始时间')
        
    return len(missing_fields) == 0, missing_fields


def timestamp_to_date_str(timestamp):
    """
    将时间戳转换为日期字符串，处理None的情况
    
    Args:
        timestamp: 毫秒级时间戳或None
        
    Returns:
        str: 格式化的日期字符串，如果输入为None则返回空字符串
    """
    if not timestamp:
        return ''
    try:
        # 将毫秒转换为秒
        seconds = timestamp / 1000
        date_obj = datetime.datetime.fromtimestamp(seconds)
        return date_obj.strftime('%Y-%m-%d')
    except Exception as e:
        return ''

# ====================== 工作项检查通用函数 ======================

def should_skip_workitem(issue_data):
    """
    判断是否应该跳过对工作项的检查

    Args:
        issue_data: 工作项数据

    Returns:
        bool: 是否应该跳过检查
    """
    # 检查工作项状态
    state_value = issue_data['state']['value']
    if state_value in ['已取消', '挂起中']:
        print(f"工作项状态为{state_value}，跳过检查")
        return True

    # 检查时间范围
    tech_design_start = issue_data.get('customField17425', 0)
    created_at = issue_data.get('createdAt', 0)

    # 如果技术设计开始时间存在且小于2024年12月1日，则跳过检查
    if tech_design_start and tech_design_start < TIMESTAMP_2024_12_01:
        return True
    # 如果技术设计开始时间不存在，则使用创建时间判断
    elif not tech_design_start and created_at < TIMESTAMP_2024_12_01:
        return True

    return False


def check_workitem(issue_data, issue_id, issue_type, username=None):
    """
    通用工作项检查函数，检查工作项字段完整性

    Args:
        issue_data: 工作项数据
        issue_id: 工作项ID
        issue_type: 工作项类型 ('DEVTASK' 或 'REQUIREMENT')
        username: 用户名，用于检查指派人

    Returns:
        tuple: (是否需要添加到结果, 工作项信息字典)
    """
    # 如果指定了用户名，检查指派人是否匹配
    if username and issue_data.get('assigned', '') != username:
        print(f"工作项：{issue_id}，用户{username}非指派人，不予通知")
        return False, None

    # 检查是否应该跳过
    if should_skip_workitem(issue_data):
        return False, None

    # 根据工作项类型进行不同的检查
    if issue_type == 'DEVTASK':
        is_complete, missing_fields, field_tips = check_task_fields(issue_data, issue_data['state']['value'])
        if not is_complete:
            return True, {
                'id': issue_id,
                'name': issue_data['name'],
                'projectId': issue_data.get('projectId', '32979'),
                'missing_fields': missing_fields,
                'field_tips': field_tips
            }
    elif issue_type == 'REQUIREMENT':
        is_complete, missing_fields = check_requirement_fields(issue_data, issue_id)
        if not is_complete:
            return True, {
                'id': issue_id,
                'name': issue_data['name'],
                'projectId': issue_data.get('projectId', '32979'),
                'missing_fields': missing_fields
            }

    return False, None

# ====================== 工作项完整性验证函数 ======================

def validate_issue_completeness(issue_ids, username):
    """
    验证单个用户的issues完整性并返回需要补充的信息
    
    Args:
        issue_ids: 工作项ID列表
        username: 用户名

    Returns:
        dict: 包含任务和需求的信息
    """
    user_message = {
        'DEVTASK': [],
        'REQUIREMENT': []
    }
    
    # 不再清空缓存，让不同用户共享缓存
    # global parent_requirement_cache
    # parent_requirement_cache = {}

    # 并行获取所有issue的详情
    issue_responses = get_issue_detail_parallel(issue_ids)

    # 收集所有需要检查的父需求ID
    parent_requirement_ids = []

    # 处理每个issue
    for issue_id, issue_info in issue_responses.items():
        if issue_info is None or "data" not in issue_info:
            continue

        issue_data = issue_info['data']
        issue_type = issue_data['type']

        # 如果是任务类型，收集其父需求ID
        if issue_type == 'DEVTASK' and issue_data.get('parentId'):
            parent_requirement_ids.append(issue_data.get('parentId'))

        # 检查工作项
        should_add, workitem_info = check_workitem(issue_data, issue_id, issue_type, username)
        if should_add and workitem_info:
            user_message[issue_type].append(workitem_info)
        
    # 获取并检查父需求
    if parent_requirement_ids:
        parent_responses = get_issue_detail_parallel(parent_requirement_ids)

        for parent_id, parent_info in parent_responses.items():
            if parent_info is None or "data" not in parent_info:
                continue

            parent_data = parent_info['data']

            # 跳过非需求类型的父工作项
            if parent_data['type'] != "REQUIREMENT":
                continue

            # 检查父需求
            should_add, workitem_info = check_workitem(parent_data, parent_id, "REQUIREMENT")

            # 如果需要添加，检查是否已经添加过相同的需求
            if should_add and workitem_info:
                already_added = any(req['id'] == parent_id for req in user_message['REQUIREMENT'])
                if not already_added:
                    user_message['REQUIREMENT'].append(workitem_info)

    return user_message

# ====================== 信号处理函数 ======================

def signal_handler(signal, frame):
    """处理中断信号，优雅退出程序"""
    print('\n程序退出中...')
    sys.exit(0)


# ====================== 主函数 ======================

if __name__ == '__main__':
    print(get_ones_open_api_token())