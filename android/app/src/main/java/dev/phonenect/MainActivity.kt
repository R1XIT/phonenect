package dev.phonenect

import android.app.Activity
import android.app.AlertDialog
import android.content.ClipboardManager
import android.content.Intent
import android.content.pm.PackageManager
import android.graphics.Typeface
import android.net.Uri
import android.os.Build
import android.os.Bundle
import android.os.PowerManager
import android.provider.Settings
import android.text.InputType
import android.util.TypedValue
import android.view.Gravity
import android.view.View
import android.widget.Button
import android.widget.EditText
import android.widget.LinearLayout
import android.widget.ScrollView
import android.widget.TextView
import android.widget.Toast
import okhttp3.OkHttpClient
import java.lang.ref.WeakReference

/** Настройка: подключение к ПК и разрешения, без которых автоматика не работает. */
class MainActivity : Activity() {
    companion object {
        private const val PICK_FILES = 2
        private var current = WeakReference<MainActivity>(null)

        /** Служба сообщает о смене статуса — перерисовываем экран, если он открыт. */
        fun refresh() {
            current.get()?.let { it.runOnUiThread { it.render() } }
        }
    }

    private lateinit var prefs: Prefs
    private lateinit var root: LinearLayout

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        prefs = Prefs(this)
        root = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            setPadding(dp(20), dp(24), dp(20), dp(24))
        }
        setContentView(ScrollView(this).apply {
            fitsSystemWindows = true
            addView(root)
        })
        handleLink(intent)
    }

    override fun onNewIntent(intent: Intent) {
        super.onNewIntent(intent)
        handleLink(intent)
    }

    override fun onResume() {
        super.onResume()
        current = WeakReference(this)
        if (prefs.configured) SyncService.start(this)
        render()
    }

    override fun onPause() {
        super.onPause()
        current.clear()
    }

    override fun onWindowFocusChanged(hasFocus: Boolean) {
        super.onWindowFocusChanged(hasFocus)
        // Приложение на экране — буфер читать можно: заодно отправим его на ПК.
        if (hasFocus) getSystemService(ClipboardManager::class.java).primaryClip?.let { SyncService.instance?.sendClip(it) }
    }

    private fun handleLink(intent: Intent?) {
        val link = intent?.data?.toString() ?: return
        connect(link)
    }

    /** Подключение по ссылке: сначала скачиваем и сверяем сертификат ПК, потом сохраняем. */
    private fun connect(link: String) {
        val pc = PcList.parseLink(link)
        if (pc == null) {
            val text = if (PcList.isOldLink(link)) "Старая ссылка — отсканируйте QR-код с ПК заново" else "Не похоже на адрес Phonenect"
            Toast.makeText(this, text, Toast.LENGTH_LONG).show()
            return
        }
        Thread {
            val result = try {
                prefs.add(pc.copy(ca = Tls.fetchCa(OkHttpClient(), pc.url, pc.fp)))
                null
            } catch (e: Tls.Mismatch) {
                e.message
            } catch (e: Exception) {
                "Не удалось связаться с ПК: он включён и в той же сети Wi-Fi?"
            }
            runOnUiThread {
                if (result == null) {
                    adding = false
                    restartSync()
                    Toast.makeText(this, "Подключено к ${pc.name}", Toast.LENGTH_SHORT).show()
                } else {
                    Toast.makeText(this, result, Toast.LENGTH_LONG).show()
                }
                render()
            }
        }.start()
    }

    /** Служба держит связь с одним, активным ПК — после смены ПК запускаем её заново. */
    private fun restartSync() {
        SyncService.stop(this)
        if (prefs.configured) SyncService.start(this)
    }

    // ---------- экран ----------

    private fun render() {
        root.removeAllViews()
        text("Phonenect", 26f, bold = true)
        val service = SyncService.instance
        val pcs = prefs.pcs
        val active = pcs.active
        val status = when {
            active == null -> "Не подключено к ПК"
            active.needsRepair -> "○ «${active.name}» подключён до шифрования — подключите заново (QR-код или «Настроить Android по USB»)"
            service?.revoked == true -> "○ «${active.name}» отключил это устройство — подключите его заново (QR-код или «Настроить Android по USB»)"
            service?.connected == true -> "● Общий буфер с «${active.name}»"
            else -> "○ Нет связи с «${active.name}» (${active.url.removePrefix("https://")}). Проверьте, что ПК в той же сети Wi-Fi."
        }
        text(status, 15f).setPadding(0, dp(6), 0, dp(18))

        if (active == null) {
            section("Подключение")
            addPcForm()
            return
        }

        section("Компьютеры")
        for (pc in pcs.items) pcRow(pc, pc.id == active.id)
        if (adding) addPcForm() else button("Добавить компьютер") {
            adding = true
            render()
        }

        section("С ПК на телефон")
        check(
            ok = true,
            title = "Работает автоматически",
            hint = "Скопировали на ПК — через мгновение это в буфере телефона.",
        )
        if (Build.VERSION.SDK_INT >= 33) {
            val granted = checkSelfPermission(android.Manifest.permission.POST_NOTIFICATIONS) == PackageManager.PERMISSION_GRANTED
            check(granted, "Уведомление о работе", "Без него Android может остановить синхронизацию.") {
                requestPermissions(arrayOf(android.Manifest.permission.POST_NOTIFICATIONS), 1)
            }
        }
        val power = getSystemService(PowerManager::class.java)
        check(
            power.isIgnoringBatteryOptimizations(packageName),
            "Работа в фоне",
            "Разрешите не экономить батарею на Phonenect, иначе связь будет рваться.",
        ) {
            startActivity(Intent(Settings.ACTION_REQUEST_IGNORE_BATTERY_OPTIMIZATIONS, Uri.parse("package:$packageName")))
        }

        section("С телефона на ПК")
        val logs = checkSelfPermission(android.Manifest.permission.READ_LOGS) == PackageManager.PERMISSION_GRANTED
        check(
            logs,
            "Отслеживание копирования",
            if (logs) "Включено." else "Один раз: включите на телефоне «Отладку по USB», подключите его кабелем к ПК " +
                "и в трее Phonenect выберите «Настроить Android по USB».",
        )
        check(
            Settings.canDrawOverlays(this),
            "Чтение буфера из фона",
            "Разрешение «Поверх других приложений»: окно на долю секунды открывается, чтобы прочитать буфер.",
        ) {
            startActivity(Intent(Settings.ACTION_MANAGE_OVERLAY_PERMISSION, Uri.parse("package:$packageName")))
        }
        text("Пока настройка не закончена, буфер уходит на ПК, когда вы открываете это приложение.", 13f, muted = true)
            .setPadding(0, dp(8), 0, 0)

        section("Файлы")
        text("Файлы с ПК сами скачиваются в «Загрузки/Phonenect»: скопируйте файл в проводнике (Ctrl+C) " +
            "или выберите в трее «Отправить файлы на телефон». С телефона на ПК — «Поделиться» → «На ПК» в любом приложении " +
            "или кнопка ниже. Файл окажется в папке «Загрузки\\Phonenect» на ПК и в его буфере (Ctrl+V).", 14f, muted = true)
        button("Отправить файлы на ПК") {
            startActivityForResult(
                Intent(Intent.ACTION_OPEN_DOCUMENT).addCategory(Intent.CATEGORY_OPENABLE).setType("*/*")
                    .putExtra(Intent.EXTRA_ALLOW_MULTIPLE, true),
                PICK_FILES,
            )
        }

    }

    private var adding = false

    private fun addPcForm() {
        text("На ПК: значок Phonenect в трее → «Подключить телефон». Отсканируйте QR-код и на открывшейся " +
            "странице нажмите «Подключить приложение». Новый ПК добавится к списку.")
        text("Или вставьте адрес Phonenect со страницы (раздел «Адрес Phonenect»):").setPadding(0, dp(12), 0, dp(4))
        val input = EditText(this).apply {
            hint = "https://192.168.0.10:8765/api/clip?t=…"
            inputType = InputType.TYPE_TEXT_VARIATION_URI
            setSingleLine()
        }
        root.addView(input)
        button("Подключить") { connect(input.text.toString()) }
    }

    /** Строка списка ПК: касание делает ПК активным, «Удалить» забывает его. */
    private fun pcRow(pc: Pc, isActive: Boolean) {
        val row = LinearLayout(this).apply {
            orientation = LinearLayout.HORIZONTAL
            gravity = Gravity.CENTER_VERTICAL
            setPadding(0, dp(6), 0, dp(6))
            if (!isActive) setOnClickListener {
                prefs.activate(pc.id)
                restartSync()
                Toast.makeText(this@MainActivity, "Теперь общий буфер с «${pc.name}»", Toast.LENGTH_SHORT).show()
                render()
            }
        }
        val texts = LinearLayout(this).apply { orientation = LinearLayout.VERTICAL }
        texts.addView(label((if (isActive) "● " else "○ ") + pc.name, 16f, bold = isActive))
        val hint = when {
            pc.needsRepair -> "нужно переподключить — отсканируйте QR-код с ПК"
            isActive && SyncService.instance?.revoked == true -> "ПК отключил это устройство — подключите заново (QR-код с ПК)"
            isActive -> pc.url.removePrefix("https://") + " · активный"
            else -> pc.url.removePrefix("https://") + " · коснитесь, чтобы переключиться"
        }
        texts.addView(label(hint, 14f, muted = true))
        row.addView(texts, LinearLayout.LayoutParams(0, LinearLayout.LayoutParams.WRAP_CONTENT, 1f))
        row.addView(Button(this).apply {
            text = "Удалить"
            setOnClickListener {
                AlertDialog.Builder(this@MainActivity)
                    .setMessage("Забыть «${pc.name}»? Чтобы подключиться снова, понадобится QR-код с этого ПК.")
                    .setPositiveButton("Удалить") { _, _ ->
                        prefs.remove(pc.id)
                        if (isActive) restartSync()
                        render()
                    }
                    .setNegativeButton("Отмена", null)
                    .show()
            }
        })
        root.addView(row)
    }

    @Suppress("OVERRIDE_DEPRECATION", "DEPRECATION") // Activity без AndroidX: другого способа получить выбор нет
    override fun onActivityResult(requestCode: Int, resultCode: Int, data: Intent?) {
        super.onActivityResult(requestCode, resultCode, data)
        if (requestCode != PICK_FILES || resultCode != RESULT_OK || data == null) return
        val clip = data.clipData
        val uris = if (clip != null) (0 until clip.itemCount).map { clip.getItemAt(it).uri } else listOfNotNull(data.data)
        if (uris.isEmpty()) return
        SyncService.sendFiles(this, uris)
        Toast.makeText(this, "Отправляю на ПК…", Toast.LENGTH_SHORT).show()
    }

    private fun section(title: String) {
        text(title.uppercase(), 13f, bold = true, muted = true).setPadding(0, dp(22), 0, dp(8))
    }

    private fun check(ok: Boolean, title: String, hint: String, fix: (() -> Unit)? = null) {
        val row = LinearLayout(this).apply {
            orientation = LinearLayout.HORIZONTAL
            gravity = Gravity.CENTER_VERTICAL
            setPadding(0, dp(6), 0, dp(6))
        }
        val texts = LinearLayout(this).apply { orientation = LinearLayout.VERTICAL }
        texts.addView(label((if (ok) "✓ " else "✗ ") + title, 16f, bold = true))
        texts.addView(label(hint, 14f, muted = true))
        row.addView(texts, LinearLayout.LayoutParams(0, LinearLayout.LayoutParams.WRAP_CONTENT, 1f))
        if (!ok && fix != null) {
            row.addView(Button(this).apply {
                text = "Разрешить"
                setOnClickListener { fix() }
            })
        }
        root.addView(row)
    }

    private fun button(title: String, onClick: () -> Unit) {
        root.addView(Button(this).apply {
            text = title
            setOnClickListener { onClick() }
        })
    }

    private fun text(value: String, size: Float = 15f, bold: Boolean = false, muted: Boolean = false): TextView =
        label(value, size, bold, muted).also { root.addView(it) }

    private fun label(value: String, size: Float, bold: Boolean = false, muted: Boolean = false) = TextView(this).apply {
        text = value
        setTextSize(TypedValue.COMPLEX_UNIT_SP, size)
        if (bold) typeface = Typeface.DEFAULT_BOLD
        if (muted) alpha = 0.65f
        textAlignment = View.TEXT_ALIGNMENT_VIEW_START
    }

    private fun dp(v: Int) = (v * resources.displayMetrics.density).toInt()
}
