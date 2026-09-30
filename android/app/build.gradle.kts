plugins {
    // В AGP 9 Kotlin встроен — отдельный плагин kotlin-android не нужен.
    id("com.android.application")
}

android {
    namespace = "dev.phonenect"
    compileSdk = 36

    defaultConfig {
        applicationId = "dev.phonenect"
        minSdk = 26
        targetSdk = 36
        versionCode = 1
        versionName = "1.0"
    }

    buildTypes {
        release {
            isMinifyEnabled = true
            proguardFiles(getDefaultProguardFile("proguard-android-optimize.txt"))
            // Приложение ставится вручную, без магазина — подписываем отладочным ключом.
            signingConfig = signingConfigs.getByName("debug")
        }
    }
}

dependencies {
    implementation("com.squareup.okhttp3:okhttp:5.3.2")
}
