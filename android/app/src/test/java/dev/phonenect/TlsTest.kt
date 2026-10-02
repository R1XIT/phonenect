package dev.phonenect

import org.junit.Assert.assertEquals
import org.junit.Assert.assertNotEquals
import org.junit.Test

class TlsTest {
    private fun resource(name: String) = javaClass.classLoader!!.getResource(name)!!.readText()

    @Test
    fun fingerprintMatchesPc() {
        assertEquals(resource("ca.fp").trim(), Tls.fingerprint(resource("ca.pem")))
    }

    @Test
    fun otherCertificateHasOtherFingerprint() {
        assertNotEquals(resource("ca.fp").trim(), Tls.fingerprint(resource("other.pem")))
    }
}
