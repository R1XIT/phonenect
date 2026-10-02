package dev.phonenect

import com.sun.net.httpserver.HttpServer
import okhttp3.OkHttpClient
import org.junit.After
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNotEquals
import org.junit.Assert.fail
import org.junit.Test
import java.net.InetSocketAddress

class TlsTest {
    private fun resource(name: String) = javaClass.classLoader!!.getResource(name)!!.readText()

    private val fp get() = resource("ca.fp").trim()

    private var server: HttpServer? = null

    /** Простой HTTP-сервер вместо ПК: fetchCa ходит по адресу из ссылки, схема для проверки не важна. */
    private fun serve(code: Int, body: ByteArray): String {
        val s = HttpServer.create(InetSocketAddress("127.0.0.1", 0), 0)
        s.createContext("/ca.crt") { exchange ->
            exchange.sendResponseHeaders(code, body.size.toLong())
            exchange.responseBody.use { it.write(body) }
        }
        s.start()
        server = s
        return "http://127.0.0.1:${s.address.port}"
    }

    @After
    fun stop() {
        server?.stop(0)
        server = null
    }

    private inline fun <reified T : Throwable> assertThrows(block: () -> Unit): T {
        try {
            block()
        } catch (e: Throwable) {
            if (e is T) return e
            throw AssertionError("ожидали ${T::class.simpleName}, получили $e", e)
        }
        fail("ожидали ${T::class.simpleName}")
        throw IllegalStateException()
    }

    @Test
    fun fingerprintMatchesPc() {
        assertEquals(fp, Tls.fingerprint(resource("ca.pem")))
    }

    @Test
    fun otherCertificateHasOtherFingerprint() {
        assertNotEquals(fp, Tls.fingerprint(resource("other.pem")))
    }

    @Test
    fun fetchCaReturnsPemWhenFingerprintMatches() {
        val url = serve(200, resource("ca.pem").toByteArray())
        assertEquals(resource("ca.pem"), Tls.fetchCa(OkHttpClient(), url, fp))
    }

    @Test
    fun fetchCaAcceptsUppercaseFingerprint() {
        val url = serve(200, resource("ca.pem").toByteArray())
        assertEquals(resource("ca.pem"), Tls.fetchCa(OkHttpClient(), url, fp.uppercase()))
    }

    @Test
    fun fetchCaRejectsOtherCertificate() {
        val url = serve(200, resource("other.pem").toByteArray())
        val e = assertThrows<Tls.Mismatch> { Tls.fetchCa(OkHttpClient(), url, fp) }
        assertEquals("Сертификат не совпадает — подключайтесь из дома и проверьте отпечаток на странице ПК", e.message)
    }

    @Test
    fun fetchCaErrorStatusIsUnreachable() {
        val url = serve(404, "нет".toByteArray())
        val e = assertThrows<Tls.Unreachable> { Tls.fetchCa(OkHttpClient(), url, fp) }
        assertEquals("Не удалось связаться с ПК: он включён и в той же сети Wi-Fi?", e.message)
    }

    @Test
    fun fetchCaGarbageIsUnreachable() {
        val url = serve(200, "<html>не сертификат</html>".toByteArray())
        assertThrows<Tls.Unreachable> { Tls.fetchCa(OkHttpClient(), url, fp) }
    }

    @Test
    fun fetchCaTooBigBodyIsUnreachable() {
        val url = serve(200, ByteArray(70_000) { 'A'.code.toByte() })
        assertThrows<Tls.Unreachable> { Tls.fetchCa(OkHttpClient(), url, fp) }
    }

    @Test
    fun fetchCaNoServerIsUnreachable() {
        val url = serve(200, ByteArray(0))
        stop()
        assertThrows<Tls.Unreachable> { Tls.fetchCa(OkHttpClient(), url, fp) }
    }
}
