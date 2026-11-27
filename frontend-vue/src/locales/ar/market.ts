/**
 * سوق الموارد (/market). Keys mirror `zh-CN/market.ts` one for one.
 *
 * The same three rules drive the wording: the source is a first-class concept,
 * an unsigned item is a notice (not a blocker), and an item installed from an
 * external source starts disabled — the user must be told where to enable it.
 */
export default {
  title: 'سوق الموارد',
  // تسمية قصيرة لقائمة التنقّل: العنوان الكامل يُقتطع في سطر واحد
  nav: 'السوق',
  subtitle: 'تصفّح الوكلاء / المهارات / المعرفة / MCP حسب المصدر وثبّتها على الجهاز',
  search_placeholder: 'ابحث بالاسم أو الوصف أو المعرّف',
  tags_all: 'كل الوسوم',

  kind: {
    all: 'الكل',
    agent: 'الوكلاء',
    skill: 'المهارات',
    knowledge: 'مجموعة مستندات',
    mcp: 'MCP',
  },

  source: {
    builtin: 'المصدر المدمج',
    type_builtin: 'مدمج',
    type_operator: 'المشغّل',
    type_user: 'المستخدم',
    count: '{n} عنصر',
    disabled_source: 'معطّل',
    error_source: 'فشل الجلب',
    stale_source: 'قد يكون قديمًا',
  },

  unsigned_badge: 'غير موقَّع',
  unsigned_hint: 'لا يحمل هذا العنصر توقيع الناشر، لذا لا يمكن التحقّق من سلامة المحتوى — تأكّد من ثقتك بالمصدر قبل التثبيت.',

  installed_badge: 'مثبَّت',
  installed_disabled_badge: 'مثبَّت (معطّل)',

  enable: 'تمكين',
  disable: 'تعطيل',
  enable_confirm_title: 'تمكين «{name}»؟',
  enable_confirm_hint:
    'عند التمكين يُعاد التحقّق من سلامة المحتوى (يُعاد جلب البيان ومقارنة البصمة والتوقيع) وتُفحص الأذونات المطلوبة؛ وبعدها فقط يُحقن المحتوى أو يُنفَّذ أو يُوصَل.',
  disable_confirm_title: 'تعطيل «{name}»؟',
  disable_confirm_hint: 'يسري التعطيل فورًا ويسحب أيضًا الأذونات المرتبطة به. أعد التمكين والتفويض عند الحاجة.',
  enable_success: 'تم تمكين «{name}»',
  disable_success: 'تم تعطيل «{name}»',
  quarantine_hint: 'مثبَّت لكنه غير مُمكَّن: لا يُحقن ولا يُنفَّذ ولا يُوصَل بعد. راجع المصدر والأذونات ثم مكّنه.',

  permission: {
    env: 'يتطلب متغيرات بيئة: {keys}',
    model: 'يتطلب نموذجًا متاحًا',
    approval: 'يتطلب تفويضًا لكل إضافة في صفحة الإضافات',
    command: 'يشغّل أمر MCP محليًا',
    fs: 'يصل إلى الملفات: {paths}',
    net: 'وصول شبكي: {hosts}',
    shell: 'ينفّذ أوامر محلية',
    undeclared: 'الأذونات غير معلنة (تُعامَل بالأشد)',
    none: 'لا يتطلب أذونات إضافية',
  },

  install: 'تثبيت',
  installing: 'جارٍ التثبيت…',
  install_confirm_title: 'تثبيت «{name}»؟',
  install_confirm_hint: 'يمكنك إلغاء التثبيت في أي وقت. إذا احتاج العنصر إلى بيانات اعتماد، فاملأ متغيّرات البيئة أدناه أولًا.',
  install_confirm_permissions: "الأذونات المطلوبة: {summary}",
  uninstall: 'إلغاء التثبيت',
  uninstalling: 'جارٍ إلغاء التثبيت…',
  uninstall_confirm_title: 'إلغاء تثبيت «{name}»؟',
  install_success: 'تم تثبيت «{name}»',
  uninstall_success: 'تم إلغاء تثبيت «{name}»',
  already_installed: 'هذا العنصر مثبَّت بالفعل',
  installed_disabled_hint: 'تم التثبيت لكنه معطّل — فعّله من الصفحة المطابقة',

  detail: {
    title: 'تفاصيل العنصر',
    version: 'الإصدار',
    author: 'المؤلف',
    homepage: 'الصفحة الرئيسية',
    manifest_url: 'عنوان البيان',
    permissions_label: 'المصدر والأذونات',
    requires_model: 'يحتاج هذا العنصر إلى نموذج مُهيّأ ومتاح',
    requires_approval: 'لا يزال يحتاج إلى تفويض في صفحة الإضافات قبل أن يعمل فعلًا',
    requires_env: 'متغيّرات البيئة المطلوبة',
    requires_env_hint: 'تُحفظ في الذاكرة لهذه الجلسة فقط — ولا تُكتب أبدًا في التخزين المحلي أو على القرص.',
    collection_documents: 'تحتوي المجموعة على {n} مستند/مستندات',
    collection_hint: 'عند التثبيت تُجلب كل المصادر أدناه وتُستورد إلى قاعدة المعرفة. لا يُبنى الفهرس تلقائيًا — شغّله من صفحة قاعدة المعرفة.',
    no_description: 'لا يوجد وصف',
  },

  empty: {
    title: 'لا توجد عناصر في هذه الفئة',
    hint: 'جرّب مصدرًا أو نوعًا آخر، أو عدّل كلمة البحث',
  },

  load_failed: 'تعذّر تحميل دليل السوق',
  load_failed_hint: 'المصدر المدمج يعمل دون اتصال، لذا يبقى متاحًا عادةً؛ قد تحتاج المصادر الخارجية إلى إصلاح الشبكة أو الإعداد.',

  warn: {
    source_unreachable: 'المصدر «{source}» غير قابل للوصول — لم يُجلب أيّ محتوى هذه المرة',
    source_invalid: 'المصدر «{source}» لم يُرجع دليل سوق صالحًا',
    source_timeout: 'انتهت مهلة استجابة المصدر «{source}»',
    source_rate_limited: 'المصدر «{source}» محدود المعدّل؛ ستُعاد المحاولة لاحقًا',
    payload_too_large: 'دليل المصدر «{source}» يتجاوز حدّ الحجم وتم رفضه',
    remote_disabled: 'المصادر الخارجية غير مفعّلة، لذا لن يُجلب «{source}»',
    stale_snapshot: 'فشل جلب «{source}»؛ يُعرض آخر محتوى ناجح',
    unsigned_items: 'المصدر «{source}» يحتوي {n} عنصرًا غير موقَّع',
    items_truncated: 'المصدر «{source}» يحتوي عناصر كثيرة؛ تُعرض أول {n} فقط',
    invalid_item: 'المصدر «{source}» يحتوي عناصر بحقول غير صالحة، وتم تخطّيها',
    unknown_item_field: 'المصدر «{source}» يحتوي عناصر بحقول غير معروفة، وتم تخطّيها',
    unsupported_kind: 'المصدر «{source}» يحتوي عناصر من نوع غير مدعوم، وتم تخطّيها',
    missing_manifest_url: 'المصدر «{source}» يحتوي عناصر بلا عنوان بيان، فلا يمكن تثبيتها وتم تخطّيها',
    generic: 'المصدر «{source}» أبلغ عن مشكلة',
  },

  error: {
    user_sources_forbidden: 'هذا النشر لا يسمح بإضافة مصادر للسوق',
    source_limit_reached: 'تم بلوغ الحدّ الأقصى لمصادر السوق — احذف مصدرًا أولًا',
    conflict: 'تعارض: هذا المصدر موجود بالفعل',
    enable_denied: 'لا يمكن التمكين بعد: قد تكون المصادر الخارجية معطّلة، أو المحتوى غير مفوَّض في صفحة الإضافات',
    enable_conflict: 'هذا النوع لا يدعم التحديث في المكان — أزل التثبيت ثم أعد تثبيته',
    integrity_failed: 'فشل التحقّق من سلامة المحتوى (عدم تطابق البصمة أو التوقيع)، ورُفض التثبيت/التمكين',
    request_failed: 'فشل طلب السوق، يرجى المحاولة لاحقًا',
  },
}
