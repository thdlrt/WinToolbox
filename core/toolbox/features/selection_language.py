"""Conservative local checks for common translation targets; no model to keep resident."""
import re
import unicodedata


def is_target_language(text, target):
    target = target.strip().lower().replace('_', '-')
    letters = [c for c in text if unicodedata.category(c).startswith('L')]
    if not letters:
        return False
    han = sum('\u3400' <= c <= '\u9fff' or '\U00020000' <= c <= '\U0002fa1f' for c in letters)
    kana = sum('\u3040' <= c <= '\u30ff' or '\uff66' <= c <= '\uff9d' for c in letters)
    hangul = sum('\uac00' <= c <= '\ud7af' or '\u1100' <= c <= '\u11ff' for c in letters)
    if target.startswith('zh') or target in ('中文', '简体中文', '繁体中文', '汉语', 'chinese', 'simplified chinese', 'traditional chinese'):
        # Chinese sentences may include a product name, acronym or technical term.
        return not kana and not hangul and han > 0 and han / len(letters) >= .55
    if target.startswith('ja') or target in ('日语', '日文', 'japanese'):
        return kana > 0 and (han + kana) / len(letters) >= .8
    if target.startswith('ko') or target in ('韩语', '韩文', 'korean'):
        return hangul / len(letters) >= .8
    if target.startswith('en') or target in ('英语', '英文', 'english'):
        if any(not ('a' <= c.lower() <= 'z') for c in letters):
            return False
        words = re.findall(r"[a-z]+(?:'[a-z]+)?", text.lower())
        common = set('the this that these those is are was were be been being a an and or but with without from for to of in on at it its you your we our they their he she have has had not do does should would could can will shall please'.split())
        if len(words) == 1:
            return words[0] in common | {'hello', 'world', 'performance', 'memory', 'computer', 'translation', 'thanks', 'thank', 'yes', 'no'}
        return len(set(words) & common) >= 2 and sum(w in common for w in words) / len(words) >= .25
    # Ambiguous or unsupported languages still reach the requested translator.
    return False
