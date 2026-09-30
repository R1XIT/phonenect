package dev.phonenect

import android.content.Context
import android.net.nsd.NsdManager
import android.net.nsd.NsdServiceInfo
import android.os.Handler
import android.os.Looper

/** Ищет агент Phonenect в сети по mDNS (_phonenect._tcp) — на случай, если у ПК сменился IP. */
object Discovery {
    private const val TYPE = "_phonenect._tcp."
    private const val TIMEOUT_MS = 8_000L

    fun find(context: Context, onFound: (host: String, port: Int) -> Unit) {
        val nsd = context.getSystemService(NsdManager::class.java)
        Search(nsd, onFound).start()
    }

    private class Search(
        private val nsd: NsdManager,
        private val onFound: (String, Int) -> Unit,
    ) : NsdManager.DiscoveryListener {
        private val handler = Handler(Looper.getMainLooper())
        private var done = false

        fun start() {
            nsd.discoverServices(TYPE, NsdManager.PROTOCOL_DNS_SD, this)
            handler.postDelayed(::finish, TIMEOUT_MS)
        }

        private fun finish() {
            if (done) return
            done = true
            try {
                nsd.stopServiceDiscovery(this)
            } catch (_: Exception) {
            }
        }

        override fun onServiceFound(info: NsdServiceInfo) {
            if (done) return
            @Suppress("DEPRECATION")
            nsd.resolveService(info, object : NsdManager.ResolveListener {
                override fun onServiceResolved(resolved: NsdServiceInfo) {
                    @Suppress("DEPRECATION")
                    val host = resolved.host?.hostAddress ?: return
                    handler.post {
                        if (done) return@post
                        finish()
                        onFound(host, resolved.port)
                    }
                }

                override fun onResolveFailed(info: NsdServiceInfo, errorCode: Int) {}
            })
        }

        override fun onServiceLost(info: NsdServiceInfo) {}
        override fun onDiscoveryStarted(serviceType: String) {}
        override fun onDiscoveryStopped(serviceType: String) {}
        override fun onStartDiscoveryFailed(serviceType: String, errorCode: Int) { done = true }
        override fun onStopDiscoveryFailed(serviceType: String, errorCode: Int) {}
    }
}
