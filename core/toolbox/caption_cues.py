"""Stable caption boundaries from revisable ASR snapshots, without tail clipping."""
import re
import unicodedata
from collections import OrderedDict, deque


MAX_CUE_UNITS = 84
STABLE_SECONDS = 1.2
_ABBREVIATIONS = re.compile(r'\b(?:Mr|Mrs|Ms|Dr|Prof|Sr|Jr|St|vs|etc)\.|\b[A-Za-z]\.(?=\s|[A-Za-z])|(?<=\d)\.(?=\d)', re.I)


def normalize(text):
    return ' '.join(str(text or '').split())


def units(text):
    return sum(2 if unicodedata.east_asian_width(char) in ('W', 'F') else 1 for char in text)


def sentence_ends(text):
    protected = _ABBREVIATIONS.sub(lambda m: m[0].replace('.', '\u2024'), text)
    return [match.end() for match in re.finditer(r'[.!?]+["\u201d\u2019]*?(?=\s|$)|[。！？]+["\u201d\u2019]*', protected)]


def first_page(text, limit=MAX_CUE_UNITS):
    if units(text) <= limit:
        return text
    end, width = 0, 0
    for index, char in enumerate(text):
        cost = 2 if unicodedata.east_asian_width(char) in ('W', 'F') else 1
        if width + cost > limit:
            break
        width += cost
        end = index + 1
    prefix = text[:end]
    clauses = [m.end() for m in re.finditer(r'[,;，；：:](?=\s|$)|[，；：]', prefix)]
    if clauses and clauses[-1] >= end * .4:
        return prefix[:clauses[-1]].strip()
    if end < len(text) and not text[end].isspace() and unicodedata.east_asian_width(text[end]) not in ('W', 'F'):
        boundary = prefix.rfind(' ')
        if boundary > 0:
            end = boundary
        else:
            # A single long token is kept whole, never cut in the middle.
            following = text.find(' ', end)
            end = following if following >= 0 else len(text)
    candidate = text[:end].strip()
    if re.search(r'\b(?:Mr|Mrs|Ms|Dr|Prof|Sr|Jr)\.$', candidate, re.I) and ' ' in candidate:
        candidate = candidate.rsplit(' ', 1)[0]
    return candidate


def split_cues(text):
    text = normalize(text)
    boundaries = sentence_ends(text)
    if not boundaries or boundaries[-1] != len(text):
        boundaries.append(len(text))
    output, start = [], 0
    for end in boundaries:
        remaining = text[start:end].strip()
        while remaining:
            page = first_page(remaining)
            output.append(page)
            remaining = remaining[len(page):].strip()
        start = end
    return output


def transcript_update(previous, event, message):
    """Delta events append; .text and completed transcripts replace snapshots."""
    if event.endswith('.delta'):
        delta = message.get('delta', '')
        return previous + (delta if isinstance(delta, str) else '')
    if isinstance(message.get('transcript'), str):
        return message['transcript']
    if isinstance(message.get('text'), str) or isinstance(message.get('stash'), str):
        return str(message.get('text') or '') + str(message.get('stash') or '')
    return previous


class CueAssembler:
    """Commit confirmed prefixes once; later server finals append only the tail."""
    def __init__(self):
        self.states = OrderedDict()
        self.completed = deque(maxlen=300)
        self.cycles = 0

    @staticmethod
    def consumed_offset(committed, text):
        if text.startswith(committed):
            return len(committed)
        # Punctuation/case can be revised after a prefix was confirmed. Align the
        # already displayed prefix, rather than replaying it or chopping a word.
        token = r'[\u2e80-\u9fff\uac00-\ud7af]|[$€£]?\d+(?:[.,]\d+)*%?|[^\W\d_]+(?:[\x27’\-][^\W\d_]+)*'
        old = [match[0].casefold() for match in re.finditer(token, committed)]
        new = list(re.finditer(token, text))
        if not old or not new:
            return 0
        count = min(len(new), len(old) + max(12, len(old) // 2))
        distances = list(range(count + 1))
        for index, word in enumerate(old, 1):
            updated = [index]
            for j in range(1, count + 1):
                updated.append(min(updated[-1] + 1, distances[j] + 1,
                                   distances[j - 1] + (word != new[j - 1][0].casefold())))
            distances = updated
        # Align against a PREFIX of the new snapshot. A later matching period or
        # repeated word cannot pull the consumed boundary into a later sentence.
        ends_sentence = bool(re.search(r'[.!?。！？]["”’]*$', committed.strip()))
        def boundary_score(j):
            # Equal edit costs occur when a service inserts/removes a word.
            # Preserve a matching suffix ("very good"), then a real sentence
            # boundary ("Thanks."), before considering the old token count.
            suffix = 0
            for a, b in zip(reversed(old), reversed(new[:j])):
                if a != b[0].casefold() or suffix == 3:
                    break
                suffix += 1
            punctuation = bool(re.match(r'[.!?。！？]', text[new[j - 1].end():]))
            return distances[j], -suffix, -(ends_sentence and punctuation), abs(j - len(old)), j
        boundary = min(range(1, count + 1), key=boundary_score)
        cursor = new[boundary - 1].end()
        while cursor < len(text) and text[cursor] in '.,;:!?。！？…"\u201d\u2019':
            cursor += 1
        while cursor < len(text) and text[cursor].isspace():
            cursor += 1
        return cursor

    def accept(self, sid, text, final, start, end, now):
        text = normalize(text)
        if not text:
            return []
        previous_end = next((stamp for key, stamp in reversed(self.completed) if key == sid), None)
        if previous_end is not None and start < previous_end + .2:
            return []
        state = self.states.get(sid)
        if state is None:
            self.cycles += 1
            state = {'prefix': '', 'plain': '', 'ages': [], 'observations': 0, 'page': 0,
                     'base_id': sid if previous_end is None else f'{sid}@{self.cycles}', 'start': start}
            self.states[sid] = state
        while len(self.states) > 32:
            self.states.popitem(last=False)
        plain = re.sub(r'[^\w$%]', '', text.casefold())
        shared = 0
        for before, after in zip(state['plain'], plain):
            if before != after:
                break
            shared += 1
        state['ages'] = state['ages'][:shared] + [now] * (len(plain) - shared)
        state['plain'] = plain
        state['observations'] += 1
        offset = self.consumed_offset(state['prefix'], text)
        remaining = text[offset:].strip()
        pages = []
        if final:
            pages = split_cues(remaining)
        elif remaining:
            ends = sentence_ends(remaining)
            candidate = first_page(remaining[:ends[0]]) if ends else first_page(remaining) if units(remaining) >= MAX_CUE_UNITS + 16 else ''
            if candidate:
                boundary = text.find(candidate, offset) + len(candidate)
                plain_end = len(re.sub(r'[^\w$%]', '', text[:boundary].casefold()))
                # A newly inserted period does not reset the age of already
                # stable words; revising those words does reset their age.
                if plain_end and now - state['ages'][plain_end - 1] >= STABLE_SECONDS and state['observations'] >= 2:
                    pages = [candidate]
        result = []
        cursor = offset
        for page in pages:
            begin = text.find(page, cursor)
            begin = cursor if begin < 0 else begin
            finish = begin + len(page)
            number = state['page']
            cue_id = state['base_id'] if number == 0 else f'{state["base_id"]}:{number}'
            length = max(0, end - state['start'])
            result.append({'id': cue_id, 'utterance_id': sid, 'page_index': number, 'text': page,
                           'start': state['start'] + length * begin / max(1, len(text)),
                           'end': state['start'] + length * finish / max(1, len(text)),
                           'recognition_final': bool(final), 'duration_ms': max(2000, min(4500, 1200 + units(page) * 38))})
            state['page'] += 1
            cursor = finish
        if result:
            state['prefix'] = text[:cursor]
        if final:
            self.states.pop(sid, None)
            self.completed.append((sid, end))
        return result
