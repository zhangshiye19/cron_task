import base64
from datetime import datetime, timedelta, date
import hashlib
import hmac
import json
import time
import concurrent.futures
import requests
from chinese_calendar import is_workday
from task import get_issues_id_by_uname, push_issue_to_room, validate_issue_completeness

# 公众号ID
PUB_ID = 138648568684

# 开发者信息的appKey
CLIENT_ID = 'g250140310265012'

# 开发者信息的appToken
CLIENT_SECRET = '9700dbf5f91025c77158ca02dd059869'

# 大象消息API的域名
NEIXIN_HOST = 'https://xmapi.vip.sankuai.com'

# 向群组推送消息
PUSH_URL = '/api/pub/pushToRoom'

# 测试群ID
TOGID = 69429561756

# 触评周会晨会群ID
# TOGID = 68768344765  

# ones API域名
ONES_HOST = 'https://ones.sankuai.com'

# 工时查询
WORKTIME_URL = '/api/1.0/ones/projects/load/user/worktime'


def get_week_range():
    today = datetime.now()
    monday = today - timedelta(days=today.weekday())
    monday = monday.replace(hour=0, minute=0, second=0, microsecond=0)
    # friday = monday + datetime.timedelta(days=4)
    # friday = friday.replace(hour=23, minute=59, second=59, microsecond=0)
    return monday, today


def is_last_workday_of_week():
    today = date.today()
    
    # 如果今天不是工作日，直接返回False
    if not is_workday(today):
        return False
        
    # 检查今天到本周日的每一天
    days_until_sunday = 6 - today.weekday()
    
    # 往后检查每一天是否有工作日
    for i in range(1, days_until_sunday + 1):
        next_day = today + timedelta(days=i)
        if is_workday(next_day):
            return False
            
    return True

def get_worktime_for_task(username):
        headers = {
        'Content-Type': 'application/json',
        'Authorization': 'Basic aXRfYmluZ3FpbGluOml0X2JpbmdxaWxpbl9vbmVz'
        }
        start_time, end_time = get_week_range()
        start_timestamp = int(start_time.timestamp() * 1000)
        end_timestamp = int(end_time.timestamp() * 1000)
        logger = get_logger()
        logger.info(f"查询时间范围：{start_time} 到 {end_time}")

        params = {
            'username': username,
            'startAt': 1732982400000,   # 2024.12.1
            'endAt': end_timestamp
        }
        response = requests.get(
            ONES_HOST + WORKTIME_URL,
            headers=headers, 
            params=params,
            verify=False
        )
        # print(response.json())
        if response.status_code != 200:
            # logger.error(f"用户 {username} 工作时长查询失败")
            return None
        response_data = response.json()
        return response_data

def format_all_users_message(all_user_messages):
    """
    格式化所有用户的消息
    """
    final_message = "⚠️ 以下同学的ONES工作项辛苦补充信息，以便准确统计组内效能指标：\n"
    final_message += "═════════\n"
    
    for username, messages in all_user_messages.items():
        if not messages['DEVTASK'] and not messages['REQUIREMENT']:
            continue
            
        task_count = len(messages['DEVTASK'])
        req_count = len(messages['REQUIREMENT'])
        total_count = task_count + req_count

        final_message += f"👤 @{username}\n"
        final_message += f"📊 共有 {total_count} 个工作项需要补充信息\n"
        
        # 合并任务和需求为工作项列表
        work_items = []
        
        # 添加任务
        for task in messages['DEVTASK']:
            task_link = f"https://ones.sankuai.com/ones/product/{task.get('projectId', '32979')}/workItem/task/detail/{task['id']}"
            missing_fields = []

            # 先处理任务类型字段，如果存在
            task_type_tip = None
            if '任务类型' in task['missing_fields'] and 'field_tips' in task and '任务类型' in task['field_tips']:
                task_type_tip = f"【任务类型】{task['field_tips']['任务类型']}"

            # 处理其他缺失字段
            for field in task['missing_fields']:
                if field == '任务类型':
                    continue  # 任务类型已单独处理
                if 'field_tips' in task and field in task['field_tips']:
                    missing_fields.append(f"【{field}】{task['field_tips'][field]}")
                else:
                    missing_fields.append(f"【{field}】")

            # 组合所有提示，任务类型放在最前面
            all_fields = []
            if task_type_tip:
                all_fields.append(task_type_tip)
            all_fields.extend(missing_fields)

            missing_fields_str = "，".join(all_fields)

            # 确定是否需要添加"未填写"
            suffix = ""
            if len(all_fields) > 1 or (len(all_fields) == 1 and not task_type_tip):
                suffix = "未填写"

            work_items.append({
                'name': task['name'],
                'link': task_link,
                'missing_fields': missing_fields_str,
                'suffix': suffix,
                'type': '任务'
            })
        
        # 添加需求
        for req in messages['REQUIREMENT']:
            req_link = f"https://ones.sankuai.com/ones/product/{req.get('projectId', '32979')}/workItem/requirement/detail/{req['id']}"
            missing_fields_str = "，".join([f"【{field}】" for field in req['missing_fields']])
            work_items.append({
                'name': req['name'],
                'link': req_link,
                'missing_fields': missing_fields_str,
                'suffix': "未填写",
                'type': '需求'
            })

        # 输出所有工作项
        for idx, item in enumerate(work_items, 1):
            final_message += f"{idx}. [{item['name']}|{item['link']}]"
            final_message += f" {item['missing_fields']}"
            if item['suffix']:
                final_message += item['suffix']
            final_message += "\n"

        final_message += "═════════\n"
    
    return final_message

def validate_issue_completeness_by_username(users, room_id):
    """
    验证多个用户的issues完整性并发送消息，使用并行处理提高效率
    """
    logger = get_logger()
    all_user_messages = {}
    
    def process_single_user(username, display_name):
        """处理单个用户的任务验证"""
        # 获取用户的所有任务
        task_ids = get_issues_id_by_uname(username)
            
        issue_ids = [x['id']['value'] for x in task_ids 
                    if x['type']['value'] in ('DEVTASK', 'REQUIREMENT')]
        
        # 验证每个任务的完整性
        user_message = validate_issue_completeness(issue_ids,username)
        
        return username, display_name, user_message
    
    # 使用线程池并行处理所有用户
    with concurrent.futures.ThreadPoolExecutor(max_workers=10) as executor:
        # 提交所有用户的处理任务
        future_to_user = {
            executor.submit(process_single_user, username, display_name): (username, display_name)
            for username, display_name in users.items()
        }

        # 收集处理结果
        for future in concurrent.futures.as_completed(future_to_user):
            try:
                username, display_name, user_message = future.result()
                # 如果有需要补充的信息，添加到总消息中
                if user_message['DEVTASK'] or user_message['REQUIREMENT']:
                    all_user_messages[display_name] = user_message
            except Exception as exc:
                username, display_name = future_to_user[future]
                logger.error(f"处理用户 {display_name} 时发生错误: {exc}")

    # 如果有需要补充信息的内容，才发送消息
    if all_user_messages:
        final_message = format_all_users_message(all_user_messages)
        logger.info("准备发送消息:\n%s", final_message)

        # 将final_message分成多批，每批不得超过10000字符
        final_message_batches = []
        batch = ''
        for line in final_message.split('\n'):
            if len(batch) + len(line) <= 10000:
                batch += line + '\n'
            else:
                final_message_batches.append(batch)
                batch = line + '\n'

        if batch:
            final_message_batches.append(batch)

        for message_batch in final_message_batches:
            # 发送消息
            push_issue_to_room(room_id, message_batch)
    else:
        logger.info("所有用户的任务/需求信息都已完整，无需发送提醒")

    # 请求结束后清空缓存
    from task import parent_requirement_cache
    parent_requirement_cache.clear()
    logger.info("请求处理完成，已清空缓存")

def get_workdays_count_of_week():
    # 获取本周一
    today = datetime.now().date()
    monday = today - timedelta(days=today.weekday())
    
    # 统计本周工作日
    workdays = 0
    for i in range(7):
        current_day = monday + timedelta(days=i)
        if is_workday(current_day):
            workdays += 1
    
    return workdays


def parse_worktime_response(response_data):
    if not response_data.get('success'):
        logger = get_logger()
        logger.error(json.dumps(response_data, indent=2, ensure_ascii=False))
        return None
    
    result = {
        'totalPDActualTime': response_data['data']['totalPDActualTime'],
        'items': []
    }
    items = response_data['data'].get('items', [])
    if items:
        for item in items:
            result['items'].append({
                'name': item['name'],
                'expectTime': item['expectTime'],
                'issueUserActualWorkTime': item['issueUserActualWorkTime']
            })
    return result


def get_worktime(username):
    headers = {
        'Content-Type': 'application/json',
        'Authorization': 'Basic aXRfYmluZ3FpbGluOml0X2JpbmdxaWxpbl9vbmVz'
    }
    start_time, end_time = get_week_range()
    start_timestamp = int(start_time.timestamp() * 1000)
    end_timestamp = int(end_time.timestamp() * 1000)
    logger = get_logger()
    logger.info(f"查询时间范围：{start_time} 到 {end_time}")

    params = {
        'username': username,
        'startAt': start_timestamp,
        'endAt': end_timestamp
    }
    response = requests.get(
        ONES_HOST + WORKTIME_URL,
        headers=headers, 
        params=params,
        verify=False
    )
    response_data = response.json()
    result = parse_worktime_response(response_data)
    worktime_info = {
        'is_valid': False,
        'message': []
    }
    
    if result:
        total_worktime = result['totalPDActualTime']
        required_worktime = get_workdays_count_of_week()

        if total_worktime < required_worktime:
            worktime_info['is_valid'] = False
            worktime_info['message'].append(f"总计工时: {total_worktime} 人天")
            # worktime_info['message'].append("工作项详情:")
            # for item in result['items']:
            #     worktime_info['message'].append(f"- 名称: {item['name']}")
            #     worktime_info['message'].append(f"  实际工时: {item['issueUserActualWorkTime']}")

            logger.info(f"用户 {username} 工作项详情:")
            for item in result['items']:
                logger.info(f"- 名称: {item['name']}")
                logger.info(f"  实际工时: {item['issueUserActualWorkTime']}")
        else:
            worktime_info['is_valid'] = True

    return worktime_info


def gen_headers(client_id, client_secret, url_path, http_method):
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


def push_to_room(room_id, users):
    logger = get_logger()
    if not is_last_workday_of_week():
        logger.info("非本周最后的工作日，不推送消息")
        return
    
    # 如果room_id为空，使用默认的TOGID
    target_room_id = room_id if room_id else TOGID

    # 最后总的输出文本
    text = ""

    for username in users:
        logger.info(f"\n检查用户 {users[username]} 的工时...")
        worktime_info = get_worktime(username)
        if not worktime_info:
            logger.error(f"用户 {username} 查询失败")
            text += f"用户 {users[username]} 的工时查询失败\n"
            text += '-' * 30 + '\n'
            continue

        if not worktime_info['is_valid']:
            text += f"@{users[username]} 本周工时填写不足，请及时补充。\n"
            text += '\n'.join(worktime_info['message']) + '\n'
            text += '-' * 30 + '\n'
            
    if not text:
        logger.info("所有用户都填写了工时，不再推送消息")
        return
    
    data = {
        'fromUid': PUB_ID,
        'toGid': target_room_id,
        'messageType': 'text',
        'body': {
            'text': text
        }
    }
    headers = gen_headers(CLIENT_ID, CLIENT_SECRET, PUSH_URL, 'PUT')
    r = requests.put(
        NEIXIN_HOST + PUSH_URL, 
        headers=headers, 
        data=json.dumps(data, indent=2, ensure_ascii=False).encode('utf-8')
    )
    logger.info(text)
    logger.info(f"发送结果：{r.status_code}")
    logger.info('-' * 30)

# 添加日志配置，用于非Flask环境
import logging

def setup_logging():
    """设置日志配置，用于非Flask环境"""
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(levelname)s - %(message)s',
        handlers=[
            logging.StreamHandler(),
            logging.FileHandler('validation.log')
        ]
    )
    return logging.getLogger(__name__)

# 全局logger，兼容Flask和非Flask环境
def get_logger():
    try:
        # 如果在Flask环境中
        from flask import current_app
        return current_app.logger
    except:
        # 如果不在Flask环境中
        return setup_logging()
