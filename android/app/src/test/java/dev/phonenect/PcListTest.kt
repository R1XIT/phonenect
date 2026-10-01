package dev.phonenect

import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Test

class PcListTest {
    private val home = PcList.parseLink("phonenect://pair?url=http%3A%2F%2F192.168.0.25%3A8765&t=HOME&name=%D0%94%D0%BE%D0%BC")!!
    private val work = PcList.parseLink("http://192.168.0.40:8765/api/clip?t=WORK")!!

    @Test
    fun parsesPairLinkWithName() {
        assertEquals("http://192.168.0.25:8765", home.url)
        assertEquals("HOME", home.token)
        assertEquals("Дом", home.name)
    }

    @Test
    fun parsesApiAddressWithoutNameUsingHost() {
        assertEquals("http://192.168.0.40:8765", work.url)
        assertEquals("192.168.0.40", work.name)
    }

    @Test
    fun rejectsLinksWithoutToken() {
        assertNull(PcList.parseLink("http://192.168.0.40:8765/"))
        assertNull(PcList.parseLink("просто текст"))
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
        val moved = home.copy(url = "http://192.168.0.99:8765")
        val list = PcList().add(home).add(work).add(moved)
        assertEquals(2, list.items.size)
        assertEquals("http://192.168.0.99:8765", list.items.first { it.token == "HOME" }.url)
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
        assertEquals("http://192.168.0.77:8765", list.items.first { it.token == "HOME" }.url)
        assertEquals("http://192.168.0.40:8765", list.items.first { it.token == "WORK" }.url)
    }

    @Test
    fun readdingByAddressKeepsKnownName() {
        val named = PcList().add(work).renamed(work.id, "Ноутбук")
        val again = named.add(PcList.parseLink("http://192.168.0.41:8765/api/clip?t=WORK")!!)
        assertEquals("Ноутбук", again.active?.name)
        assertEquals("http://192.168.0.41:8765", again.active?.url)
    }

    @Test
    fun renamesPc() {
        val list = PcList().add(work).renamed(work.id, "Ноутбук")
        assertEquals("Ноутбук", list.active?.name)
    }
}
