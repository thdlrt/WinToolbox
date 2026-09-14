"""Versioned system voice catalogue; model listings are not voice listings.

Source: https://help.aliyun.com/zh/model-studio/qwen-tts-voice-list
Non-realtime synthesis table, checked 2026-09-10. Descriptions are abbreviated.
This is not an account's voice-cloning inventory or an availability probe.
"""

SOURCE = 'https://help.aliyun.com/zh/model-studio/qwen-tts-voice-list'
ROWS = [
    ('Cherry', '芊悦', '亲切女声'), ('Jennifer', '詹妮弗', '美语女声'),
    ('Aiden', '艾登', '美语男声'), ('Ryan', '甜茶', '表现力男声'),
    ('Serena', '苏瑶', '柔和女声'), ('Ethan', '晨煦', '活力男声'),
    ('Chelsie', '千雪', '动漫女声'), ('Momo', '茉兔', '俏皮女声'),
    ('Vivian', '十三', '活泼女声'), ('Moon', '月白', '爽朗男声'),
    ('Maia', '四月', '知性女声'), ('Kai', '凯', '舒缓男声'),
    ('Nofish', '不吃鱼', '生活化男声'), ('Bella', '萌宝', '年轻女声'),
    ('Katerina', '卡捷琳娜', '成熟女声'), ('Eldric Sage', '沧明子', '年长男声'),
    ('Mia', '乖小妹', '轻柔女声'), ('Mochi', '沙小弥', '童声男声'),
    ('Bellona', '燕铮莺', '洪亮女声'), ('Vincent', '田叔', '沙哑男声'),
    ('Bunny', '萌小姬', '可爱女声'), ('Neil', '阿闻', '播报男声'),
    ('Elias', '墨讲师', '讲解女声'), ('Arthur', '徐大爷', '质朴男声'),
    ('Nini', '邻家妹妹', '甜美女声'), ('Seren', '小婉', '舒缓女声'),
    ('Pip', '顽屁小孩', '男孩童声'), ('Stella', '少女阿月', '少女声'),
    ('Bodega', '博德加', '热情男声'), ('Sonrisa', '索尼莎', '开朗女声'),
    ('Alek', '阿列克', '低沉男声'), ('Dolce', '多尔切', '慵懒男声'),
    ('Sohee', '素熙', '开朗女声'), ('Ono Anna', '小野杏', '俏皮女声'),
    ('Lenn', '莱恩', '青年男声'), ('Emilien', '埃米尔', '柔和男声'),
    ('Andre', '安德雷', '沉稳男声'), ('Radio Gol', '拉迪奥', '解说男声'),
    ('Jada', '阿珍', '上海话女声'), ('Dylan', '晓东', '北京话男声'),
    ('Li', '老李', '南京话男声'), ('Marcus', '秦川', '陕西话男声'),
    ('Roy', '阿杰', '闽南语男声'), ('Peter', '李彼得', '天津话男声'),
    ('Sunny', '晴儿', '四川话女声'), ('Eric', '程川', '四川话男声'),
    ('Rocky', '阿强', '粤语男声'), ('Kiki', '阿清', '粤语女声'),
]
BASE = {'Cherry', 'Serena', 'Ethan', 'Chelsie'}
EARLY = {'Cherry', 'Ethan', 'Nofish', 'Jennifer', 'Ryan', 'Katerina', 'Elias',
         'Jada', 'Dylan', 'Li', 'Marcus', 'Roy', 'Peter', 'Sunny', 'Eric', 'Rocky', 'Kiki'}
INSTRUCT = BASE | {'Momo', 'Vivian', 'Moon', 'Maia', 'Kai', 'Nofish', 'Bella',
    'Eldric Sage', 'Mia', 'Mochi', 'Bellona', 'Vincent', 'Bunny', 'Neil', 'Elias',
    'Arthur', 'Nini', 'Seren', 'Pip', 'Stella'}


def catalogue(settings):
    values = settings.get()
    role = values.get('roles', {}).get('tts', {})
    model = role.get('model', '')
    provider = next((p for p in values.get('providers', []) if p['id'] == role.get('provider_id')), {})
    supported = set()
    if provider.get('kind') == 'dashscope':
        if model in ('qwen3-tts-flash', 'qwen3-tts-flash-2025-11-27'):
            supported = {row[0] for row in ROWS}
        elif model == 'qwen3-tts-flash-2025-09-18':
            supported = EARLY
        elif model in ('qwen3-tts-instruct-flash', 'qwen3-tts-instruct-flash-2026-01-26'):
            supported = INSTRUCT
        elif model in ('qwen-tts', 'qwen-tts-2025-04-10'):
            supported = BASE
        elif model in ('qwen-tts-latest', 'qwen-tts-2025-05-22'):
            supported = BASE | {'Jada', 'Dylan', 'Sunny'}
    return {
        'model': model, 'source': '百炼官方音色目录' if supported else '自定义音色',
        'source_url': SOURCE if provider.get('kind') == 'dashscope' else '',
        'checked_at': '2026-09-10', 'custom_allowed': True,
        'voices': [{'id': id, 'name': f'{id} · {name}', 'description': description}
                   for id, name, description in ROWS if id in supported],
        'notice': ('按当前模型显示官方系统音色；支持英语。自定义音色可手动填写 ID。' if supported
                   else '当前模型没有已核对的系统音色目录，请填写服务商提供的音色 ID。'),
    }
