"""Find unanswered bot questions in verified same-chat message history."""
from datetime import datetime
import re


def message_time(message):
    value = str(message.get('create_time', ''))
    try:
        if value.isdigit():
            timestamp = int(value)
            return timestamp / 1000 if timestamp > 10**11 else timestamp
        return datetime.fromisoformat(value).timestamp()
    except (ValueError, OverflowError, OSError):
        return 0


def own_bot(message, config):
    sender = message.get('sender') or {}
    return sender.get('sender_type') in ('app', 'bot') and sender.get('id') in (
        config['app_id'], config['bot_open_id'])


def is_acknowledgement(message, rule, config):
    text = str(message.get('content', '')).strip()
    return bool(text) and (text in (rule.get('ack_text'), config.get('ack_text')) or '思考中' in text)


def is_question(message):
    text = str(message.get('content', '')).replace('\\n', '\n')
    text = re.sub(r'https?://\S+', '', text)
    interrogative = r'(?:能否|能不能|是否|是不是|哪(?:个|些|位|种)|吗|呢)[^。！？?\n]{0,100}[？?]'
    completed_notice = re.search(r'(?:任务(?:已)?完成|(?:打包|构建|发布|部署)[^。！？?\n]{0,12}(?:已完成|完成|好(?:啦|了)?)|已(?:部署|发布))', text[:100])
    if completed_notice and not re.search(interrogative, text):
        return False
    # Look for a request for information or a real interrogative, rather than
    # treating every question mark in an FAQ or every failure notice as pending.
    for line in re.findall(r'[^。！？?\n]+[。！？?]?', text):
        if re.search(r'(?:如果|如需|若需|如遇|遇到|有问题|下载后|解压后|使用时|需要我|你可以)[^。！？?\n]{0,100}(?:请|麻烦|确认|提供)', line):
            continue
        if re.search(r'(?:请|麻烦|烦请)[^。！？?\n]{0,100}(?:确认|回复|提供|补充|告知|选择|决定|说明)', line):
            return True
        if re.search(interrogative, line):
            return True
    return False


def parent_id(message):
    return message.get('reply_to') or message.get('parent_id') or message.get('root_id') or ''


def prior_messages(messages, rule, reference):
    position = str(reference.get('message_position', ''))
    reference_time = message_time(reference)
    history = []
    for message in messages:
        if message.get('chat_id') != rule['chat_id'] or message.get('deleted'):
            continue
        if message.get('message_id') == reference.get('message_id'):
            continue
        other_position = str(message.get('message_position', ''))
        if position.isdigit() and other_position.isdigit():
            if int(other_position) >= int(position):
                continue
        elif reference_time and message_time(message) > reference_time:
            continue
        history.append(message)
    return history


def question_history(messages, rule, config, reference, cutoff):
    """Return questions and those answered by a subsequent bot follow-up.

    A user's response alone leaves a question pending until the bot follows up;
    another question in that follow-up replaces the original question.
    """
    history = prior_messages(messages, rule, reference)
    indexed = {m['message_id']: m for m in history if m.get('message_id')}
    questions = {mid: m for mid, m in indexed.items() if own_bot(m, config)
                 and not is_acknowledgement(m, rule, config)
                 and message_time(m) >= cutoff and is_question(m)}
    answered = set()
    for message in history:
        if not own_bot(message, config) or is_acknowledgement(message, rule, config):
            continue
        current = parent_id(message)
        visited = set()
        human_response = False
        while current and current in indexed and current not in visited and len(visited) < 20:
            visited.add(current)
            ancestor = indexed[current]
            if (ancestor.get('sender') or {}).get('sender_type') == 'user':
                human_response = True
            if human_response and current in questions:
                answered.add(current)
            current = parent_id(ancestor)
    return questions, answered
