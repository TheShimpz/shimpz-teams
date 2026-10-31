//! The phrase tables, each pattern in Python `re` syntax as Python declares it, read through the crate's
//! Python pattern translator. Every name is the Python name without its leading underscore.
//!
//! Two interval patterns, Chinese and Arabic, read their count as `\s*(?:(\d+)\s*)?` where Python writes
//! `\s*(\d*)\s*` (Arabic `\s+`): the same texts and spans, a count group that is `None` instead of empty when no digit
//! is written, which `_interval` reads alike. Python's form splits a run of spaces between its two `\s*` in every way
//! when the unit does not follow, so a long run would exhaust the pinned backtracking limit where Python only slows.

/// Each language's negations: one before a reading, or anywhere where the negation trails, rejects it.
pub const NEGATIONS: [(&str, &str); 8] = [
    ("pt", r"\b(?:não|nunca|nem)\b"),
    ("en", r"\b(?:not|don't|dont|never|no)\b"),
    ("es", r"\b(?:no|nunca)\b"),
    ("fr", r"\b(?:ne|pas|jamais)\b|\bn'"),
    ("de", r"\b(?:nicht|kein|keine|nie)\b"),
    ("ja", r"ない|ません"),
    ("zh", r"不|别"),
    ("ar", r"(?:^|\s)(?:لا|ليس|لن|لم)(?:\s|$)"),
];

/// Languages whose negation follows what it negates, anywhere in the sentence; elsewhere it comes before.
pub const TRAILING_NEGATION: [&str; 1] = ["ja"];

/// Each interval unit word, by the unit it names.
pub const UNIT: [(&str, &str); 46] = [
    ("segundo", "second"),
    ("minuto", "minute"),
    ("hora", "hour"),
    ("second", "second"),
    ("sec", "second"),
    ("minute", "minute"),
    ("min", "minute"),
    ("hour", "hour"),
    ("hr", "hour"),
    ("seconde", "second"),
    ("heure", "hour"),
    ("sekunde", "second"),
    ("stunde", "hour"),
    ("秒", "second"),
    ("分", "minute"),
    ("時間", "hour"),
    ("秒钟", "second"),
    ("分钟", "minute"),
    ("小时", "hour"),
    ("个小时", "hour"),
    ("ثانية", "second"),
    ("ثوان", "second"),
    ("ثواني", "second"),
    ("دقيقة", "minute"),
    ("دقائق", "minute"),
    ("ساعة", "hour"),
    ("ساعات", "hour"),
    ("dia", "day"),
    ("día", "day"),
    ("semana", "week"),
    ("day", "day"),
    ("week", "week"),
    ("jour", "day"),
    ("semaine", "week"),
    ("tag", "day"),
    ("woche", "week"),
    ("日", "day"),
    ("週間", "week"),
    ("天", "day"),
    ("周", "week"),
    ("星期", "week"),
    ("个星期", "week"),
    ("يوم", "day"),
    ("أيام", "day"),
    ("أسبوع", "week"),
    ("أسابيع", "week"),
];

/// An interval: (language, pattern with an optional count group and a unit group, or a fixed unit).
pub const INTERVALS: [(&str, &str); 18] = [
    (
        "pt",
        r"\b(?:a\s+)?cada\s+(?:(\d+)\s+)?(segundo|minuto|hora|dia|semana)s?\b",
    ),
    (
        "pt",
        r"\bde\s+(\d+)\s+em\s+\d+\s+(segundo|minuto|hora|dia|semana)s?\b",
    ),
    ("pt", r"\bde\s+()(minuto|hora)\s+em\s+(?:minuto|hora)\b"),
    ("pt", r"\btod[ao]\s+()(minuto|hora)\b"),
    (
        "en",
        r"\bevery\s+(?:(\d+)\s+)?(second|sec|minute|min|hour|hr|day|week)s?\b",
    ),
    ("en", r"\beach\s+()(second|minute|hour)\b"),
    ("en", r"\b()(hour)ly\b"),
    (
        "es",
        r"\bcada\s+(?:(\d+)\s+)?(segundo|minuto|hora|d[ií]a|semana)s?\b",
    ),
    (
        "fr",
        r"\btoutes\s+les\s+(?:(\d+)\s+)?(seconde|minute|heure|semaine)s?\b",
    ),
    ("fr", r"\btous\s+les\s+(\d+)\s+(jour)s\b"),
    ("fr", r"\bchaque\s+()(seconde|minute|heure)\b"),
    (
        "de",
        r"\balle\s+(\d+)\s+(sekunde|minute|stunde|tag|woche)(?:n|e)?\b",
    ),
    ("de", r"\bjede[nrs]?\s+()(sekunde|minute|stunde)\b"),
    ("de", r"\b()(stünd)lich\b"),
    ("ja", r"(\d+)\s*(秒|分|時間|日|週間)(?:ごと|おき|毎)"),
    ("ja", r"毎()(時|分)(?!間)"),
    (
        "zh",
        r"每(?:隔)?\s*(?:(\d+)\s*)?(秒钟|秒|分钟|个小时|小时|天|个星期|星期|周)(?![一二三四五六日天])",
    ),
    (
        "ar",
        r"كل\s+(?:(\d+)\s*)?(ثانية|ثواني|ثوان|دقيقة|دقائق|ساعة|ساعات|أيام|يوم|أسابيع|أسبوع)(?!\s+(?:ال)?(?:اثنين|ثلاثاء|أربعاء|خميس|جمعة|سبت|أحد))",
    ),
];

/// The unit words a fixed interval reads as one unit.
pub const FIXED_UNITS: [(&str, &str); 2] = [("stünd", "hour"), ("時", "hour")];

/// A day every day: (language, pattern).
pub const DAILY: [(&str, &str); 8] = [
    (
        "pt",
        r"\b(?:todo\s+dia|todos\s+os\s+dias|diariamente|cada\s+dia)\b",
    ),
    ("en", r"\b(?:every\s+day|daily|each\s+day)\b"),
    (
        "es",
        r"\b(?:todos\s+los\s+d[ií]as|cada\s+d[ií]a|diariamente)\b",
    ),
    (
        "fr",
        r"\b(?:tous\s+les\s+jours|chaque\s+jour|quotidiennement)\b",
    ),
    ("de", r"\b(?:jeden\s+tag|täglich)\b"),
    ("ja", r"毎日"),
    ("zh", r"每天|每日"),
    (
        "ar",
        r"كل\s+يوم(?!\s+(?:ال)?(?:اثنين|ثلاثاء|أربعاء|خميس|جمعة|سبت|أحد))|يومياً?",
    ),
];

/// Each weekday word, by its weekday (0 is Monday).
pub const WEEKDAYS: [(&str, i64); 57] = [
    ("segunda", 0),
    ("terça", 1),
    ("terca", 1),
    ("quarta", 2),
    ("quinta", 3),
    ("sexta", 4),
    ("sábado", 5),
    ("sabado", 5),
    ("domingo", 6),
    ("monday", 0),
    ("tuesday", 1),
    ("wednesday", 2),
    ("thursday", 3),
    ("friday", 4),
    ("saturday", 5),
    ("sunday", 6),
    ("lunes", 0),
    ("martes", 1),
    ("miércoles", 2),
    ("miercoles", 2),
    ("jueves", 3),
    ("viernes", 4),
    ("lundi", 0),
    ("mardi", 1),
    ("mercredi", 2),
    ("jeudi", 3),
    ("vendredi", 4),
    ("samedi", 5),
    ("dimanche", 6),
    ("montag", 0),
    ("dienstag", 1),
    ("mittwoch", 2),
    ("donnerstag", 3),
    ("freitag", 4),
    ("samstag", 5),
    ("sonntag", 6),
    ("月", 0),
    ("火", 1),
    ("水", 2),
    ("木", 3),
    ("金", 4),
    ("土", 5),
    ("日", 6),
    ("一", 0),
    ("二", 1),
    ("三", 2),
    ("四", 3),
    ("五", 4),
    ("六", 5),
    ("天", 6),
    ("اثنين", 0),
    ("ثلاثاء", 1),
    ("أربعاء", 2),
    ("خميس", 3),
    ("جمعة", 4),
    ("سبت", 5),
    ("أحد", 6),
];

/// A weekday every week: (language, pattern whose group names the weekday).
pub const WEEKLY: [(&str, &str); 8] = [
    (
        "pt",
        r"\b(?:tod[ao]s?\s+(?:[ao]s\s+)?|cada\s+)(segunda|terça|terca|quarta|quinta|sexta|sábado|sabado|domingo)s?\b",
    ),
    (
        "en",
        r"\b(?:every|each)\s+(monday|tuesday|wednesday|thursday|friday|saturday|sunday)\b",
    ),
    (
        "es",
        r"\b(?:todos\s+los|cada)\s+(lunes|martes|miércoles|miercoles|jueves|viernes|sábado|sabado|domingo)s?\b",
    ),
    (
        "fr",
        r"\b(?:chaque|tous\s+les)\s+(lundi|mardi|mercredi|jeudi|vendredi|samedi|dimanche)s?\b",
    ),
    (
        "de",
        r"\bjede[nr]?\s+(montag|dienstag|mittwoch|donnerstag|freitag|samstag|sonntag)\b",
    ),
    ("ja", r"毎週([月火水木金土日])曜"),
    ("zh", r"每(?:周|星期|礼拜)([一二三四五六日天])"),
    (
        "ar",
        r"كل\s+(?:يوم\s+)?(?:ال)?(اثنين|ثلاثاء|أربعاء|خميس|جمعة|سبت|أحد)",
    ),
];

/// A day of every month: (language, pattern whose group is the day).
pub const MONTHLY: [(&str, &str); 8] = [
    (
        "pt",
        r"\b(?:todo\s+m[eê]s|cada\s+m[eê]s|mensalmente)\b.*?\bdia\s+(\d{1,2})\b",
    ),
    (
        "en",
        r"\b(?:every\s+month|each\s+month|monthly)\b.*?\b(?:on\s+the\s+)?(\d{1,2})(?:st|nd|rd|th)\b",
    ),
    (
        "es",
        r"\b(?:cada\s+mes|todos\s+los\s+meses|mensualmente)\b.*?\bd[ií]a\s+(\d{1,2})\b",
    ),
    (
        "fr",
        r"\b(?:chaque\s+mois|tous\s+les\s+mois|mensuellement)\b.*?\ble\s+(\d{1,2})\b",
    ),
    (
        "de",
        r"\b(?:jeden\s+monat|monatlich)\b.*?\bam\s+(\d{1,2})\b",
    ),
    ("ja", r"毎月(\d{1,2})日"),
    ("zh", r"每月(\d{1,2})[日号號]"),
    ("ar", r"كل\s+شهر.*?(?:يوم|اليوم)\s+(\d{1,2})"),
];

/// Times of day, most specific first: hour, optional minute, and an optional half of the day.
pub const TIMES: [&str; 7] = [
    r"(\d{1,2})(?::(\d{2}))?\s*(am|pm|a\.m\.|p\.m\.)",
    r"(\d{1,2})\s*[:h]\s*(\d{2})()",
    r"(\d{1,2})()\s*(?:h|horas?|hrs?)\b()",
    r"(\d{1,2})(?::(\d{2}))?\s*uhr()",
    r"(\d{1,2})時(?:(\d{1,2})分)?()",
    r"(\d{1,2})[点點](?:(\d{1,2})分)?()",
    r"(?:\bàs|\bas|\bat|\ba\s+las|\ba\s+la|\bà|\bum|الساعة)\s+(\d{1,2})(?::(\d{2}))?()(?!\s*\d)",
];

/// What each run does with its result: (kind, language, pattern).
pub const OUTPUTS: [(&str, &str, &str); 40] = [
    (
        "show",
        "pt",
        r"\b(?:mostrar|mostre|exibir|exiba)\s+(?:sempre|em\s+todas\s+as\s+execuções)\b",
    ),
    (
        "result",
        "pt",
        r"\b(?:mostrar|mostre|exibir|exiba)\s+os?\s+resultados?\b",
    ),
    (
        "changes",
        "pt",
        r"\b(?:só|somente|apenas)\s+(?:quando|se)\s+(?:mudar|alterar|houver\s+mudança)",
    ),
    (
        "condition",
        "pt",
        r"\b(?:sempre|toda\s+vez)\s+que\s+(?:algo\s+|houver\s+)?(?:mudar|alterar|mudança)",
    ),
    (
        "none",
        "pt",
        r"\b(?:não|sem)\s+(?:precisa\s+)?(?:mostrar|exibir)\b",
    ),
    (
        "show",
        "en",
        r"\b(?:show|display)\s+(?:me\s+)?(?:it\s+)?(?:every\s+(?:run|time)|always)\b",
    ),
    (
        "result",
        "en",
        r"\b(?:show|display)\s+(?:me\s+)?the\s+results?\b",
    ),
    (
        "changes",
        "en",
        r"\bonly\s+(?:(?:when|if)\s+(?:it\s+)?changes|on\s+changes?)\b",
    ),
    (
        "condition",
        "en",
        r"\b(?:every\s+time|whenever|always\s+when)\s+(?:it|something|anything)\s+changes\b",
    ),
    (
        "none",
        "en",
        r"\b(?:don't|do\s+not|no\s+need\s+to)\s+show\b|\bshow\s+nothing\b",
    ),
    (
        "show",
        "es",
        r"\b(?:mostrar|muestra|muéstrame)\s+(?:siempre|en\s+cada\s+ejecución)",
    ),
    (
        "result",
        "es",
        r"\b(?:mostrar|muestra|muéstrame)\s+(?:el\s+resultado|los\s+resultados)",
    ),
    (
        "changes",
        "es",
        r"\bsolo\s+(?:cuando\s+cambie|si\s+cambia)\b",
    ),
    (
        "condition",
        "es",
        r"\b(?:siempre|cada\s+vez)\s+que\s+(?:algo\s+)?cambie\b",
    ),
    ("none", "es", r"\b(?:no|sin)\s+mostrar\b"),
    (
        "show",
        "fr",
        r"\b(?:afficher|affiche|montre(?:-moi)?)\s+(?:toujours|à\s+chaque\s+exécution)",
    ),
    (
        "result",
        "fr",
        r"\b(?:afficher|affiche|montre(?:-moi)?)\s+les?\s+résultats?",
    ),
    (
        "changes",
        "fr",
        r"\b(?:seulement|uniquement)\s+(?:en\s+cas\s+de\s+changement|quand\s+(?:ça|il|elle)\s+change)",
    ),
    (
        "condition",
        "fr",
        r"\b(?:toujours\s+(?:quand|lorsque)\s+|(?:à\s+)?chaque\s+fois\s+(?:que\s+|qu'))(?:ça|il|elle|cela|quelque\s+chose)\s+change",
    ),
    (
        "none",
        "fr",
        r"\bne\s+(?:pas|rien)\s+afficher\b|\bsans\s+afficher\b",
    ),
    ("show", "de", r"\b(?:immer|bei\s+jedem\s+lauf)\s+anzeigen\b"),
    ("result", "de", r"\bzeig\s+mir\s+das\s+ergebnis\b"),
    (
        "changes",
        "de",
        r"\bnur\s+(?:bei\s+änderung|wenn\s+(?:es\s+)?sich\s+(?:etwas\s+)?ändert)",
    ),
    (
        "condition",
        "de",
        r"\b(?:immer|anzeigen),?\s+wenn\s+(?:es\s+)?sich\s+(?:etwas\s+)?ändert",
    ),
    ("none", "de", r"\b(?:nicht|nichts)\s+anzeigen\b"),
    ("show", "ja", r"毎回表示"),
    ("result", "ja", r"結果を表示"),
    (
        "changes",
        "ja",
        r"(?:変更|変化)(?:時|したとき)のみ|変わったときだけ",
    ),
    (
        "condition",
        "ja",
        r"(?:変わる|(?:変更|変化)(?:が)?ある)たびに",
    ),
    ("none", "ja", r"表示しない|表示不要"),
    ("show", "zh", r"每次(?:运行都)?显示"),
    ("result", "zh", r"显示结果"),
    ("changes", "zh", r"(?:仅|只)在变化时"),
    ("condition", "zh", r"每(?:次|当)(?:有|发生)?变化时?"),
    ("none", "zh", r"不(?:用)?显示"),
    ("show", "ar", r"اعرض\s+(?:في\s+كل\s+تشغيل|دائم)"),
    ("result", "ar", r"اعرض\s+النتيجة"),
    ("changes", "ar", r"فقط\s+عند\s+التغيير"),
    (
        "condition",
        "ar",
        r"(?:دائماً?\s+)?كلما\s+(?:ت|ي)?تغير|دائماً?\s+عندما\s+(?:ت|ي)?تغير",
    ),
    ("none", "ar", r"لا\s+تعرض|بدون\s+عرض"),
];

/// A whole zone token: never part of a longer word, path, or offset.
pub const ZONE: &str = r#"(?<![\w/.:+-])([A-Za-z][A-Za-z_+-]*(?:/[A-Za-z][A-Za-z0-9_+-]*){1,2}|UTC)(?=$|[\s,;!?)\]}\"'»”，。！？；、]|[.:](?:\s|$))"#;

/// What separates two clauses offered as alternatives.
pub const ALTERNATIVE: &str = r"[,;，、،；]|\b(?:ou|or|o|oder)\b|または|或|还是|أو";

/// A Routine the person asks for, named by its noun in each language.
pub const ROUTINE_NOUNS: [(&str, &str); 8] = [
    ("pt", r"\brotinas?\b"),
    ("en", r"\broutines?\b"),
    ("es", r"\brutinas?\b"),
    ("fr", r"\broutines?\b"),
    ("de", r"\broutinen?\b"),
    ("ja", r"ルーティン"),
    ("zh", r"例行任务|定时任务"),
    ("ar", r"(?:^|\s)(?:ال)?روتين(?:\s|$)"),
];

/// How a question asks what Team reads from the person's own words, by language.
pub const ASK_PHRASES: [&str; 30] = [
    r"\bcom que frequência (?:a rotina |ela |isso )?(?:deve |vai |precisa )?(?:rodar|executar|ser executada|se repetir)",
    r"\b(?:a|de) cada quanto tempo (?:a rotina |ela |isso )?(?:deve |vai )?(?:rodar|executar|se repetir)",
    r"\b(?:em )?que (?:horas?|horário) (?:a rotina |ela |isso )?(?:deve |vai )?(?:rodar|executar)",
    r"\bquantas vezes por (?:dia|hora|semana|mês) (?:a rotina |ela |isso )?(?:deve |vai |pode )?(?:rodar|executar)",
    r"\blimite de execuções\b",
    r"\b(?:quer|prefere|deseja) receber (?:o|os) resultados?\b|\bo que (?:\w+ ){0,4}com (?:o|os) resultados?\b",
    r"\bhow (?:often|frequently) (?:should|do you want|would you like|will) (?:it|the routine|this) (?:to )?(?:run|repeat)",
    r"\b(?:at )?what time (?:should|do you want|would you like|will) (?:it|the routine|this) (?:to )?(?:run|start)",
    r"\bhow many times (?:a|per) (?:day|hour|week|month) (?:should|can|will) (?:it|the routine|this) run\b",
    r"\brun limit\b|\bhow many runs (?:a|per) day\b",
    r"\b(?:like|want) to (?:receive|get|see) the results?\b|\b(?:do|happen) (?:with|to) the results?\b",
    r"\bcon qué frecuencia (?:debe |quieres que )?(?:se ejecute|se repita|ejecutarse|correr)",
    r"\bcada cuánto (?:tiempo )?(?:debe |quieres que )?(?:se ejecute|se repita|ejecutarse)",
    r"\ba qué hora (?:debe |quieres que )?(?:se ejecute|ejecutarse|correr)",
    r"\bcuántas veces al día (?:debe |quieres que )?(?:se ejecute|ejecutarse)",
    r"\blímite de ejecuciones\b",
    r"(?:quieres|prefieres|desea) recibir (?:el|los) resultados?|\bqué \w+ con (?:el|los) resultados?",
    r"à quelle fréquence (?:doit-elle |doit-il |voulez-vous qu'elle |faut-il )?(?:s'exécuter|tourner|la lancer|se répéter)",
    r"à quelle heure (?:doit-elle |doit-il |voulez-vous qu'elle |faut-il )?(?:s'exécuter|tourner|la lancer)",
    r"\bcombien de fois par jour (?:doit-elle |doit-il )?(?:s'exécuter|tourner)",
    r"\blimite d'exécutions\b",
    r"recevoir (?:le|les) résultats?|\bque faire (?:du|des) résultats?",
    r"\bwie oft soll (?:sie|es|die routine) (?:laufen|ausgeführt werden)",
    r"\b(?:um wie viel uhr|wann) soll (?:sie|es|die routine) (?:laufen|ausgeführt werden)",
    r"\bwie viele male pro tag soll (?:sie|es|die routine) (?:laufen|ausgeführt werden)",
    r"\bausführungslimit|\blimit der ausführungen\b",
    r"\bwie möchte\w* \w+ das ergebnis|\bwas soll mit dem ergebnis",
    r"どのくらいの頻度で実行|何時に実行|1日に何回実行|実行回数の上限|結果をどのように|結果をどう",
    r"多久(?:运行|执行)一次|几点(?:运行|执行)|每天(?:运行|执行)几次|(?:运行|执行)次数上限|如何接收结果|结果(?:怎么|如何)处理|如何处理结果",
    r"كم مرة (?:يجب أن )?(?:يعمل|تعمل|يتم تشغيل)|في أي وقت (?:يجب أن )?(?:يعمل|تعمل|يتم تشغيل)|حد التشغيل|كيف تريد استلام النتيجة|ماذا (?:أفعل|نفعل) بالنتيجة",
];

/// The same questions asked bare: they count only as the whole question.
pub const BARE_ASKS: [&str; 10] = [
    r"com que frequência|(?:a|de) cada quanto tempo|de quanto em quanto tempo|quantas vezes por (?:dia|hora|semana)",
    r"how (?:often|frequently)|how many times (?:a|per) (?:day|hour|week)",
    r"con qué frecuencia|cada cuánto(?: tiempo)?|cuántas veces al día",
    r"à quelle fréquence|tous les combien|combien de fois par jour",
    r"wie oft|wie viele male pro tag",
    r"どのくらいの頻度|1日に何回",
    r"多久一次|多长时间一次|每天几次|一天几次",
    r"كم مرة|كل كم",
    r"(?:at )?what time|(?:a |em )?que (?:horas|horário)|a qué hora|à quelle heure|um wie viel uhr",
    r"何時に?|几点|في أي وقت",
];
