# ربط خدمة العملاء بواتساب

المسار الجديد `/webhooks/whatsapp` يستقبل رسائل WhatsApp Business Platform بعد التحقق من توقيع Meta. صفحة `/admin/whatsapp-support` تعرض الإعدادات الناقصة والمتابعات وتتيح إيقاف الردود واستئناف كل محادثة. لا توجد رسائل تسويقية أو طلبات مورد ضمن هذا الربط.

## الإعداد في حسابك

1. استخدم تطبيق Meta المرتبط بحساب WhatsApp Business ورقم دعم المتجر. الرقم العادي وحده لا يوفر بيانات Cloud API.
2. أضف في **Production** بمشروع `shoppy-store` داخل فريق `shoppy5` على Vercel:
   - `SHOPY_WA_ACCESS_TOKEN`: توكن رسمي صالح لصلاحيات إرسال حسابك؛ خزّنه كسر.
   - `SHOPY_WA_PHONE_NUMBER_ID`: معرف الرقم من Meta، وليس رقم الموبايل.
   - `SHOPY_WA_APP_SECRET`: سر التطبيق للتحقق من أصالة الرسائل؛ خزّنه كسر.
   - `SHOPY_WA_VERIFY_TOKEN`: قيمة عشوائية خاصة طولها 24 حرفًا على الأقل؛ خزّنها كسر واستخدم نفس القيمة عند تعريف الـ webhook.
   - `SHOPY_WA_GRAPH_VERSION`: نسخة Graph المدعومة في إعداد تطبيقك، مثل الصيغة `vNN.N`؛ راجع النسخة الفعلية، ولا تنسخ المثال الحرفي.
   - `SHOPY_PUBLIC_URL=https://shoppy-store-one.vercel.app`
   - `SHOPY_WA_SUPPORT_ENABLED=1` بعد تجهيز اختبار الربط.
3. أعد نشر الإنتاج لتصل القيم الجديدة للخادم. المعاينات لا ترسل رسائل حتى مع وجود قيم الإنتاج.
4. أضف في إعدادات Webhooks في Meta عنوان `https://shoppy-store-one.vercel.app/webhooks/whatsapp` ونفس Verify Token، واشترك في حقل `messages` واربط التطبيق بحساب WhatsApp المطلوب.
5. من حساب الأدمن افتح `/admin/whatsapp-support` وشغّل الردود بعد اكتمال الإعدادات. هذا الزر لا يتحقق من صلاحية التوكن عند Meta؛ نجاح الاختبار التالي هو الدليل.
6. أرسل رسالة اختبار من رقمك إلى رقم دعم المتجر، ثم تحقق من تسجيل الرسالة والرد وحالة التسليم. جرّب طلب «خدمة العملاء» ثم تأكد أن المحادثة أصبحت متابعة بشرية، وأن زر الإيقاف يمنع الرد على رسائل جديدة.

إعدادات دعم العملاء مستقلة عن `SHOPY_WA_ENABLED` الخاص بإشعارات الطلبات. لا تغيّر إعدادات الإشعارات ولا تُرسل رسائلها القديمة عند تشغيل الدعم.

## قاعدة البيانات

أضف الجدولين والفهارس الموجودة في `whatsapp_support.SCHEMA` إلى PostgreSQL قبل نشر الكود. الجداول خاصة بالخادم؛ فعّل RLS واسحب صلاحيات `anon` و`authenticated` عنها. لا توجد سياسات تمنح واجهة Supabase العامة قراءة محادثات العملاء. الأمر `init()` يجهّز نفس الجداول للاختبارات وتشغيل SQLite المحلي، ولا يُشغَّل لترحيل كامل قاعدة الإنتاج.

## حدود النسخة الحالية

الردود تعتمد على مطابقة اسم المنتج وقسمه وإظهار سعره وروابطه وقواعد الموقع، دون نموذج AI توليدي أو تكلفة API لنموذج. لا تقدم أسعار شحن نهائية أو وعد توصيل خارج حسابات الموقع، ولا تكشف بيانات طلبات خاصة. الحالات غير المحسومة تتحول لمتابعة بشرية.

تُحجز هوية كل رسالة في قاعدة البيانات قبل إرسال الرد لمنع التكرار. إذا انتهى الاتصال بالمزوّد دون نتيجة واضحة تُوقف المحادثة للمراجعة ولا يُعاد الإرسال آليًا. إذا توقف الخادم بين الحجز وتحديث النتيجة قد تظهر حالة قيد التجهيز/الإرسال؛ راجع Meta قبل أي تدخل. معالجة الرسائل تتم داخل الطلب بزمن اتصال بالمزوّد محدود، وليست عاملًا دائم التشغيل.

## مصادر رسمية

- https://developers.facebook.com/documentation/business-messaging/whatsapp/get-started
- https://developers.facebook.com/documentation/business-messaging/whatsapp/webhooks/overview
- https://developers.facebook.com/documentation/business-messaging/whatsapp/webhooks/create-webhook-endpoint/
- https://developers.facebook.com/documentation/business-messaging/whatsapp/messages/send-messages
- https://developers.facebook.com/documentation/business-messaging/whatsapp/pricing
