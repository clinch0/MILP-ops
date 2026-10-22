"""Conservative automatic screening; recognizer agreement does not prove naturalness."""
import re
from .normalize import normalize_text

# Changing what can pass invalidates every earlier release certificate.
QUALITY_POLICY_VERSION = "ru-clinic-critical-4"
_NUMBER_WORDS = frozenset("ноль один одна два две три четыре пять шесть семь восемь девять десять одиннадцать двенадцать тринадцать четырнадцать пятнадцать шестнадцать семнадцать восемнадцать девятнадцать двадцать тридцать сорок пятьдесят шестьдесят семьдесят восемьдесят девяносто сто двести триста четыреста пятьсот шестьсот семьсот восемьсот девятьсот тысяча тысячи тысяч минус плюс".split())
_CALENDAR_WORDS = frozenset("январь января февраль февраля март марта апрель апреля май мая июнь июня июль июля август августа сентябрь сентября октябрь октября ноябрь ноября декабрь декабря понедельник понедельника вторник вторника среда среду среды четверг четверга пятница пятницу пятницы суббота субботу субботы воскресенье воскресенья".split())
_ORDINAL = re.compile(r"(?:перв|втор|трет|треть|четверт|пят|шест|седьм|восьм|девят|десят|одиннадцат|двенадцат|тринадцат|четырнадцат|пятнадцат|шестнадцат|семнадцат|восемнадцат|девятнадцат|двадцат|тридцат|сороков|пятидесят|шестидесят|семидесят|восьмидесят|девяност|сот|двухсот|трехсот|четырехсот|пятисот|шестисот|семисот|восьмисот|девятисот|тысячн|двухтысячн)(?:ый|ий|ой|ое|ее|ая|яя|ую|юю|ого|его|ому|ему|ом|ем|ых|их|ым|им|ые|ие)")
_STATUS = ((re.compile(r"отмен.*"), "cancelled"),
           (re.compile(r"подтвержд.*|подтверд.*"), "confirmed"),
           (re.compile(r"записан.*|оформлен.*"), "booked"),
           (re.compile(r"перенес.*|перенос.*"), "rescheduled"))
_POLARITY = frozenset("не ни нет нельзя без запрещено запрещается разрешено разрешается можно невозможно обязательно никогда только до после перед раньше позже минимум максимум менее более".split())


_HOUR_GENITIVE = dict(zip(
    'нуля одного двух трёх четырёх пяти шести семи восьми девяти десяти одиннадцати двенадцати тринадцати четырнадцати пятнадцати шестнадцати семнадцати восемнадцати девятнадцати двадцати'.split(),
    range(21)))
_HOUR_GENITIVE.update({'двадцати одного':21,'двадцати двух':22,'двадцати трёх':23})
_HOUR_PATTERN = '|'.join(re.escape(w) for w in sorted(_HOUR_GENITIVE,key=len,reverse=True))
_HOUR_RANGE = re.compile(r'\bс\s+('+_HOUR_PATTERN+r'|\d{1,2}:\d{2})\s+до\s+('+_HOUR_PATTERN+r'|\d{1,2}:\d{2})(?:\s+часов)?\b',re.I)


def tokens(text):
    # ASR may format «с десяти до шестнадцати часов» as «с 10:00 до 16:00».
    # Normalize only an explicit clock range; keep minutes, order and the
    # bounding prepositions, so altered times or до/после still fail closed.
    def clock_range(match):
        def clock(value):
            return f'{_HOUR_GENITIVE[value.lower()]:02d}:00' if value.lower() in _HOUR_GENITIVE else value
        return 'с '+clock(match[1])+' до '+clock(match[2])
    text=_HOUR_RANGE.sub(clock_range,str(text))
    return re.findall('[а-яё]+', normalize_text(text).lower().replace('ё', 'е').replace('+', ''))


def critical_numbers(words):
    return [word for word in words if word in _NUMBER_WORDS or word in _CALENDAR_WORDS or _ORDINAL.fullmatch(word)]


def statuses(words):
    return [label for word in words for pattern, label in _STATUS if pattern.fullmatch(word)]


def polarity(words):
    # Bind negation, permission and preparation timing to the following word.
    # «До»/«после», «никогда»/«только» are material even in a long message.
    # Moving «не» from
    # «подтверждайте» to «отменяйте» changes meaning even when counts match.
    return [(word, words[i + 1] if i + 1 < len(words) else '')
            for i, word in enumerate(words) if word in _POLARITY]


def compare(expected, actual):
    a, b = tokens(expected), tokens(actual)
    prev = list(range(len(b) + 1))
    for i, x in enumerate(a, 1):
        row = [i]
        for j, y in enumerate(b, 1):
            row.append(min(row[-1] + 1, prev[j] + 1, prev[j - 1] + (x != y)))
        prev = row
    wer = prev[-1] / max(1, len(a))
    numbers_match = critical_numbers(a) == critical_numbers(b)
    polarity_match = polarity(a) == polarity(b)
    status_match = statuses(a) == statuses(b)
    return {'wer': round(wer, 4), 'numbers_match': numbers_match,
            'polarity_match': polarity_match, 'status_match': status_match, 'quality_policy_version': QUALITY_POLICY_VERSION,
            'passed': wer <= .15 and numbers_match and polarity_match and status_match}
