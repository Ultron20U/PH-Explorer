plugins {
    id("com.android.application")
    id("org.jetbrains.kotlin.android")
    id("com.chaquo.python")
}

// Keep embedded Python bundle synced with root server.py & index.html
val pySrcDir = file("src/main/python")
val rootServerPy = file("../../server.py")
val rootIndexHtml = file("../../index.html")
if (rootServerPy.exists() && rootIndexHtml.exists()) {
    pySrcDir.mkdirs()
    rootServerPy.copyTo(file("src/main/python/server.py"), overwrite = true)
    rootIndexHtml.copyTo(file("src/main/python/index.html"), overwrite = true)
}

android {
    namespace = "app.phexplorer"
    compileSdk = 34

    defaultConfig {
        applicationId = "app.phexplorer"
        minSdk = 26
        targetSdk = 34
        versionCode = 3
        versionName = "1.2.0"

        ndk {
            abiFilters += listOf("arm64-v8a", "armeabi-v7a", "x86_64")
        }
    }

    signingConfigs {
        val ksPath = System.getenv("PHE_KEYSTORE_FILE")
        if (!ksPath.isNullOrBlank() && file(ksPath).exists()) {
            create("release") {
                storeFile = file(ksPath)
                storePassword = System.getenv("PHE_KEYSTORE_PASSWORD") ?: "phexplorer123"
                keyAlias = System.getenv("PHE_KEY_ALIAS") ?: "phexplorer"
                keyPassword = System.getenv("PHE_KEY_PASSWORD") ?: "phexplorer123"
            }
        }
    }

    buildTypes {
        release {
            isMinifyEnabled = false
            isShrinkResources = false
            signingConfig = signingConfigs.findByName("release") ?: signingConfigs.getByName("debug")
        }
    }

    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }

    kotlinOptions {
        jvmTarget = "17"
    }
}

chaquopy {
    defaultConfig {
        version = "3.11"
        val pyBin = System.getenv("PHE_BUILD_PYTHON")
        if (!pyBin.isNullOrBlank()) {
            buildPython(pyBin)
        }
        pyc {
            src = false
        }
    }
}

dependencies {
    implementation("androidx.core:core-ktx:1.13.1")
    implementation("androidx.appcompat:appcompat:1.7.0")
    implementation("androidx.activity:activity-ktx:1.9.2")
    implementation("androidx.webkit:webkit:1.11.0")
}
