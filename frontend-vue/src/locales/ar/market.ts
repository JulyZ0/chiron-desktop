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
    knowledge: 'المعرفة',
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

  install: 'تثبيت',
  installing: 'جارٍ التثبيت…',
  install_confirm_title: 'تثبيت «{name}»؟',
  install_confirm_hint: 'يمكنك إلغاء التثبيت في أي وقت. إذا احتاج العنصر إلى بيانات اعتماد، فاملأ متغيّرات البيئة أدناه أولًا.',
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
    requires_model: 'يحتاج هذا العنصر إلى نموذج مُهيّأ ومتاح',
    requires_approval: 'لا يزال يحتاج إلى تفويض في صفحة الإضافات قبل أن يعمل فعلًا',
    requires_env: 'متغيّرات البيئة المطلوبة',
    requires_env_hint: 'تُحفظ في الذاكرة لهذه الجلسة فقط — ولا تُكتب أبدًا في التخزين المحلي أو على القرص.',
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
    request_failed: 'فشل طلب السوق، يرجى المحاولة لاحقًا',
  },
}
