import java.security.MessageDigest
import java.time.Instant
import org.gradle.api.file.FileCollection
import org.gradle.api.tasks.SourceSetContainer
import org.gradle.api.tasks.JavaExec
import org.gradle.api.tasks.bundling.Jar
import org.gradle.api.tasks.compile.JavaCompile

plugins {
    java
    `maven-publish`
}

group = "dev.oraja.training"
version = providers.gradleProperty("irVersion").orElse("0.1.0").get()

java {
    toolchain {
        languageVersion.set(JavaLanguageVersion.of(17))
    }
    withSourcesJar()
}

val sourceSets = the<SourceSetContainer>()
val compat = sourceSets.create("beatorajaCompat") {
    java.srcDir("src/compat/java")
}

val beatorajaJarProperty = providers.gradleProperty("beatorajaJar")
    .orElse(providers.environmentVariable("BEATORAJA_JAR"))
val beatorajaApiVersion = providers.gradleProperty("beatorajaApiVersion")
    .orElse("master@721856fbb431")

val apiClasspath: FileCollection = if (beatorajaJarProperty.isPresent) {
    files(beatorajaJarProperty.get())
} else {
    files(compat.output)
}

dependencies {
    // The upstream beatoraja runtime owns these classes.  They are never
    // bundled into this plugin JAR.
    compileOnly(apiClasspath)
    testRuntimeOnly(apiClasspath)
}

tasks.named<JavaCompile>(compat.compileJavaTaskName).configure {
    options.release.set(17)
    options.encoding = "UTF-8"
}

sourceSets.named("main") {
    compileClasspath = compileClasspath + apiClasspath
}
sourceSets.named("test") {
    compileClasspath = compileClasspath + apiClasspath
    runtimeClasspath = runtimeClasspath + apiClasspath
}

tasks.withType<JavaCompile>().configureEach {
    options.release.set(17)
    options.encoding = "UTF-8"
    options.isDeprecation = true
}

tasks.named<Jar>("jar") {
    archiveBaseName.set("oraja-training-ir")
    isPreserveFileTimestamps = false
    isReproducibleFileOrder = true
    manifest {
        attributes["Implementation-Title"] = "oraja-training beatoraja IR"
        attributes["Implementation-Version"] = project.version.toString()
        attributes["Automatic-Module-Name"] = "dev.oraja.training.ir"
    }
}

val sourceJar = tasks.named<Jar>("sourcesJar") {
    archiveBaseName.set("oraja-training-ir")
    isPreserveFileTimestamps = false
    isReproducibleFileOrder = true
}

val releaseDir = layout.buildDirectory.dir("release")

val licenseNotice = tasks.register("licenseNotice") {
    outputs.dir(releaseDir)
    doLast {
        val destination = releaseDir.get().file("NOTICE").asFile
        destination.parentFile.mkdirs()
        destination.writeText(
            """oraja-training beatoraja IR ${project.version}

SPDX-License-Identifier: AGPL-3.0-only
This client is distributed under the GNU Affero General Public License v3.0.
License text: https://www.gnu.org/licenses/agpl-3.0.html

Compatibility boundary:
  exch-bms2/beatoraja, GPL-3.0-only, commit 721856fbb431
  https://github.com/exch-bms2/beatoraja
The beatoraja API/runtime is compile-only and is not bundled in this JAR.

Build timestamp (metadata only): ${Instant.now()}
""".trimIndent() + "\n"
        )
    }
}

val checksums = tasks.register("checksums") {
    dependsOn(tasks.named("jar"), sourceJar)
    outputs.dir(releaseDir)
    doLast {
        val destination = releaseDir.get().asFile
        destination.mkdirs()
        val artifacts = listOf(
            tasks.named<Jar>("jar").get().archiveFile.get().asFile,
            sourceJar.get().archiveFile.get().asFile,
        )
        artifacts.forEach { artifact ->
            val digest = MessageDigest.getInstance("SHA-256")
                .digest(artifact.readBytes())
                .joinToString("") { byte -> "%02x".format(byte) }
            destination.resolve("${artifact.name}.sha256").writeText("$digest  ${artifact.name}\n")
        }
    }
}

val sbom = tasks.register("sbom") {
    dependsOn(tasks.named("jar"))
    outputs.dir(releaseDir)
    doLast {
        val destination = releaseDir.get().file("sbom.cdx.json").asFile
        destination.parentFile.mkdirs()
        val dependency = if (beatorajaJarProperty.isPresent) {
            """{"type":"library","name":"beatoraja","version":"${beatorajaApiVersion.get()}","scope":"provided","licenses":[{"license":{"id":"GPL-3.0-only"}}]}"""
        } else {
            """{"type":"library","name":"beatoraja-api-compat-stubs","version":"test-only","scope":"test","licenses":[{"license":{"id":"NO-RUNTIME-COMPONENT"}}]}"""
        }
        destination.writeText(
            """{
  "bomFormat": "CycloneDX",
  "specVersion": "1.5",
  "version": 1,
  "metadata": {"component": {"type": "application", "name": "oraja-training-ir", "version": "${project.version}"}},
  "components": [$dependency]
}
""".trimIndent() + "\n"
        )
    }
}

val releaseArtifacts = tasks.register("releaseArtifacts") {
    dependsOn(tasks.named("jar"), sourceJar, checksums, sbom, licenseNotice)
    doLast {
        logger.lifecycle("Local artifacts written to ${releaseDir.get().asFile}")
    }
}

val contractTest = tasks.register<JavaExec>("contractTest") {
    dependsOn(tasks.named("testClasses"))
    group = "verification"
    description = "Runs dependency-free IR contract and spool tests."
    classpath = sourceSets.test.get().runtimeClasspath
    mainClass.set("dev.oraja.training.ir.ContractTestMain")
    jvmArgs("-ea")
}

tasks.named("check") {
    dependsOn(contractTest)
}

publishing {
    publications {
        create<MavenPublication>("ir") {
            from(components["java"])
            pom {
                name.set("oraja-training beatoraja IR")
                description.set("Durable, allowlisted beatoraja IR client")
                licenses {
                    license {
                        name.set("GNU Affero General Public License v3.0")
                        url.set("https://www.gnu.org/licenses/agpl-3.0.html")
                    }
                }
            }
        }
    }
}
