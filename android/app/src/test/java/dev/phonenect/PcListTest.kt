package dev.phonenect

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

class PcListTest {
    private val fp = "a".repeat(64)
    private val home = PcList.parseLink("phonenect://pair?url=https%3A%2F%2F192.168.0.25%3A8765&t=HOME&name=%D0%94%D0%BE%D0%BC&fp=$fp")!!
    private val work = PcList.parseLink("https://192.168.0.40:8765/api/clip?t=WORK&fp=$fp")!!

    @Test
    fun parsesPairLinkWithName() {
        assertEquals("https://192.168.0.25:8765", home.url)
        assertEquals("HOME", home.token)
        assertEquals("Дом", home.name)
    }

    @Test
    fun parsesApiAddressWithoutNameUsingHost() {
        assertEquals("https://192.168.0.40:8765", work.url)
        assertEquals("192.168.0.40", work.name)
    }

    @Test
    fun rejectsLinksWithoutToken() {
        assertNull(PcList.parseLink("https://192.168.0.40:8765/"))
        assertNull(PcList.parseLink("просто текст"))
    }

    @Test
    fun rejectsOldLinksWithoutEncryption() {
        assertNull(PcList.parseLink("phonenect://pair?url=http%3A%2F%2F192.168.0.25%3A8765&t=HOME"))
        assertNull(PcList.parseLink("https://192.168.0.40:8765/api/clip?t=WORK"))
        assertTrue(PcList.isOldLink("http://192.168.0.40:8765/api/clip?t=WORK"))
        assertFalse(PcList.isOldLink("просто текст"))
    }

    @Test
    fun keepsFingerprint() {
        assertEquals(fp, home.fp)
    }

    @Test
    fun idMatchesServerHashOfToken() {
        // config.pc_id("HOME") на ПК: sha256("HOME")[:12]
        assertEquals("36e65c39f83b", PcList.idFor("HOME"))
    }

    @Test
    fun addingSecondPcKeepsFirstAndMakesNewActive() {
        val list = PcList().add(home).add(work)
        assertEquals(listOf("HOME", "WORK"), list.items.map { it.token })
        assertEquals("WORK", list.active?.token)
    }

    @Test
    fun addingSamePcAgainUpdatesAddressInsteadOfDuplicating() {
        val moved = home.copy(url = "https://192.168.0.99:8765")
        val list = PcList().add(home).add(work).add(moved)
        assertEquals(2, list.items.size)
        assertEquals("https://192.168.0.99:8765", list.items.first { it.token == "HOME" }.url)
        assertEquals("HOME", list.active?.token)
    }

    @Test
    fun switchesActivePc() {
        val list = PcList().add(home).add(work).activate(home.id)
        assertEquals("HOME", list.active?.token)
    }

    @Test
    fun removingActivePcActivatesRemainingOne() {
        val list = PcList().add(home).add(work).remove(work.id)
        assertEquals(listOf("HOME"), list.items.map { it.token })
        assertEquals("HOME", list.active?.token)
        assertNull(PcList().add(home).remove(home.id).active)
    }

    @Test
    fun newIpChangesOnlyThatPc() {
        val list = PcList().add(home).add(work).moved(home.id, "192.168.0.77", 8765)
        assertEquals("https://192.168.0.77:8765", list.items.first { it.token == "HOME" }.url)
        assertEquals("https://192.168.0.40:8765", list.items.first { it.token == "WORK" }.url)
    }

    @Test
    fun readdingByAddressKeepsKnownName() {
        val named = PcList().add(work).renamed(work.id, "Ноутбук")
        val again = named.add(PcList.parseLink("https://192.168.0.41:8765/api/clip?t=WORK&fp=$fp")!!)
        assertEquals("Ноутбук", again.active?.name)
        assertEquals("https://192.168.0.41:8765", again.active?.url)
    }

    @Test
    fun renamesPc() {
        val list = PcList().add(work).renamed(work.id, "Ноутбук")
        assertEquals("Ноутбук", list.active?.name)
    }

    @Test
    fun needsRepairWithoutFingerprintCaOrHttps() {
        val full = home.copy(ca = "PEM")
        assertFalse(full.needsRepair)
        assertTrue(full.copy(fp = "").needsRepair)
        assertTrue(full.copy(ca = "").needsRepair)
        assertTrue(full.copy(url = "http://192.168.0.25:8765").needsRepair)
    }

    @Test
    fun readdingWithSameFingerprintKeepsKnownCa() {
        val list = PcList().add(home.copy(ca = "PEM")).add(home.copy(url = "https://192.168.0.26:8765"))
        assertEquals("PEM", list.active?.ca)
    }

    @Test
    fun readdingWithOtherFingerprintDropsKnownCa() {
        val list = PcList().add(home.copy(ca = "PEM")).add(home.copy(fp = "b".repeat(64)))
        assertEquals("", list.active?.ca)
    }

    @Test
    fun uppercaseFingerprintIsLowercased() {
        val pc = PcList.parseLink("https://192.168.0.40:8765/api/clip?t=WORK&fp=${"AB".repeat(32)}")
        assertEquals("ab".repeat(32), pc?.fp)
    }

    @Test
    fun nonHexFingerprintIsRejected() {
        assertNull(PcList.parseLink("https://192.168.0.40:8765/api/clip?t=WORK&fp=${"z".repeat(64)}"))
    }

    @Test
    fun oldLinkDetection() {
        assertTrue(PcList.isOldLink("https://192.168.0.40:8765/api/clip?t=WORK"))
        assertTrue(PcList.isOldLink("https://192.168.0.40:8765/api/clip?t=WORK&fp=${"z".repeat(64)}"))
        assertTrue(PcList.isOldLink("  http://192.168.0.40:8765/api/clip?t=WORK"))
        assertTrue(PcList.isOldLink(" phonenect://pair?url=http%3A%2F%2F192.168.0.25%3A8765&t=HOME"))
        assertFalse(PcList.isOldLink("https://192.168.0.40:8765/api/clip?t=WORK&fp=$fp"))
        assertFalse(PcList.isOldLink("https://192.168.0.40:8765/"))
    }
}
