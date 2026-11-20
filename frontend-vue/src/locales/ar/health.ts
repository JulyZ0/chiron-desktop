/** health 域（ar，RTL）：系统自检面板，键与 zh-CN/health.ts 一一对应。 */
export default {
  title: 'الفحص الذاتي للنظام',
  entry: 'الفحص الذاتي',
  refresh: 'تحديث',
  fix: 'إصلاح',
  expand: 'توسيع تفاصيل الفحص الذاتي',
  collapse: 'طي تفاصيل الفحص الذاتي',
  updated: 'آخر تحديث {time}',
  loading: 'جارٍ قراءة نتائج الفحص الذاتي…',
  empty: 'لا توجد عناصر فحص للعرض',
  error: 'تعذّرت قراءة بعض بيانات الفحص: {msg}',
  hint: 'فحص للقراءة فقط ولا يغيّر أي إعداد',

  status_ok: 'سليم',
  status_warn: 'انتباه',
  status_error: 'خطأ',
  status_unknown: 'غير معروف',

  item_schema: 'إصدار المخطط',
  item_tables: 'الجداول المطلوبة',
  item_knowledge: 'خدمة المعرفة',
  item_checkpoints: 'حماية نقاط الحفظ',
  item_deps: 'اعتماديات Python',
  item_models: 'إعداد النموذج',
  item_pricing: 'تغطية الأسعار',

  detail_models_none: 'لم يتم تمكين أي نموذج بعد',
  detail_models_count: 'تم تمكين {count} نموذجًا',
  detail_models_unknown: 'تعذّرت قراءة قائمة النماذج',
  detail_pricing_all: '{priced}/{total} نموذجًا لديها سعر',
  detail_pricing_partial: '{unpriced} من {total} نموذجًا بلا سعر',
  detail_pricing_empty: 'لا توجد استدعاءات قابلة للتسعير خلال {days} يومًا',
}
