"""Conservative request recognition and receipt identity; no external actions."""
from config import get_config
import hashlib
import re



def effective_text(message):
    return message.get('text') or (message.get('dialog') or {}).get('text') or (message.get('hyperLink') or {}).get('text') or ''


def request_mode(message):
    text = effective_text(message)
    if message.get('body_type') == 7 and '简历' in text and any(w in text for w in ('同意', '索要', '想要')):
        return 'card'
    if message.get('body_type') != 1:
        return None
    # Anchored phrases deliberately exclude extra questions, external addresses,
    # qualifications and other documents. An LLM cannot widen this recognition.
    text = re.sub(r'[\s，,。.!！?？~～、]', '', text)
    greeting = r'(?:您好|你好|哈喽)?(?:请|麻烦|辛苦)?(?:您|你)?(?:能否|能不能|可以|方便)?'
    resume = r'(?:您的|你的)?(?:附件)?(?:个人)?简历'
    ending = r'(?:给我|给我们|过来)?(?:看看|看下|看一下)?(?:可以吗|好吗|吗|吧)?(?:谢谢)?'
    patterns = [
        greeting + r'(?:给我|给我们)?(?:发|发送|提供|发来)(?:一下|下|一份|份|个|一版)?' + resume + ending,
        greeting + r'(?:把|将)?' + resume + r'(?:发|发送)(?:一份|份|一下|下)?(?:给我|给我们|过来)' + ending,
    ]
    return 'text' if any(re.fullmatch(pattern, text) for pattern in patterns) else None


def resume_key(row, request_id):
    name = hashlib.sha256(get_config().attachment_name.encode()).hexdigest()[:12]
    return f"resume:{row['job_id']}:{row['uid']}:{request_id}:{name}"


def official_receipt(message):
    if message.get('body_type') != 12:
        return False
    text = re.sub(r'\s+', '', effective_text(message))
    name = re.sub(r'\s+', '', get_config().attachment_name)
    return bool(name and re.fullmatch(r'您的附件简历' + re.escape(name) + r'(?:\.pdf)?已发送给Boss(?:点击查看附件)?', text))


def after_request(message, request):
    try:
        return (int(message['message_id']) > int(request['message_id']) and
                int(message['timestamp']) >= int(request['timestamp']))
    except (KeyError, ValueError, TypeError):
        return False


def delivered_receipt(messages, request):
    return next((m for m in messages if official_receipt(m) and after_request(m, request)), None)


def resume_send_attempt(messages, request, self_uid):
    """An account-side send action is not proof of delivery, but forbids resend."""
    return next((m for m in messages if str(m.get('from_uid')) == str(self_uid)
                 and m.get('body_type') == 4 and isinstance(m.get('action'), dict)
                 and str(m['action'].get('aid')) == '38' and after_request(m, request)), None)


def superseding_request(messages, request):
    """Recognise explicit withdrawals/changed files, leaving that message unconsumed."""
    for message in messages:
        if (message.get('body_type') != 1 or
                str(message.get('from_uid')) != str(request.get('from_uid')) or
                not after_request(message, request)):
            continue
        text = re.sub(r'\s+', '', effective_text(message))
        withdrawn = re.search(r'(?:不用|不要|不需要|无需|别).{0,8}(?:发|提供|简历)', text)
        changed = ('简历' in text and re.search(r'发|提供|换|改', text) and
                   re.search(r'英文|英语|中英文|作品集|邮箱|邮件|微信|@|另一|另外|新版|更新|修改|word|docx', text, re.I))
        if withdrawn or changed:
            return message
    return None
