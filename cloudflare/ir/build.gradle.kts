import java.security.MessageDigest
import java.util.zip.ZipFile
import org.gradle.api.file.FileCollection
import org.gradle.api.tasks.SourceSetContainer
import org.gradle.api.tasks.JavaExec
import org.gradle.api.tasks.bundling.Jar
import org.gradle.api.tasks.bundling.Zip
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

if (!beatorajaJarProperty.isPresent) {
    tasks.named<JavaCompile>("compileJava").configure {
        dependsOn(tasks.named(compat.classesTaskName))
    }
    tasks.named<JavaCompile>("compileTestJava").configure {
        dependsOn(tasks.named(compat.classesTaskName))
    }
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
        attributes["Bundle-License"] = "GPL-3.0-only"
    }
    from(listOf("LICENSE", "NOTICE")) {
        into("META-INF")
    }
}

val sourceJar = tasks.named<Jar>("sourcesJar") {
    archiveBaseName.set("oraja-training-ir")
    isPreserveFileTimestamps = false
    isReproducibleFileOrder = true
    from(listOf("LICENSE", "NOTICE", "README.md", "PUBLICATION.md"))
}

val sourceBundle = tasks.register<Zip>("sourceBundle") {
    archiveBaseName.set("oraja-training-ir")
    archiveVersion.set(project.version.toString())
    archiveClassifier.set("source")
    isPreserveFileTimestamps = false
    isReproducibleFileOrder = true
    from(projectDir) {
        include("src/**")
        include("build.gradle.kts", "settings.gradle.kts", "gradle.properties")
        include("gradle/wrapper/gradle-wrapper.properties")
        include("README.md", "PUBLICATION.md", "LICENSE", "NOTICE")
        into("oraja-training-ir-${project.version}")
    }
}

val releaseDir = layout.buildDirectory.dir("release")

val licenseNotice = tasks.register("licenseNotice") {
    outputs.dir(releaseDir)
    doLast {
        val destination = releaseDir.get().asFile
        destination.mkdirs()
        file("LICENSE").copyTo(destination.resolve("LICENSE"), overwrite = true)
        file("NOTICE").copyTo(destination.resolve("NOTICE"), overwrite = true)
    }
}

val verifyLicenseBoundary = tasks.register("verifyLicenseBoundary") {
    group = "verification"
    description = "Rejects AGPL metadata or missing GPL headers in the public IR source tree."
    doLast {
        val javaSources = fileTree("src") { include("**/*.java") }.files.sorted()
        check(javaSources.isNotEmpty()) { "IR source tree is empty" }
        javaSources.forEach { source ->
            val text = source.readText()
            check(text.startsWith("/* SPDX-License-Identifier: GPL-3.0-only */")) {
                "missing GPL-3.0-only header: ${source.relativeTo(projectDir)}"
            }
            check("AGPL-3.0" !in text) {
                "AGPL metadata crossed the public GPL-only IR boundary: ${source.relativeTo(projectDir)}"
            }
        }
        for (metadata in listOf(file("README.md"), file("NOTICE"))) {
            check("SPDX-License-Identifier: AGPL-3.0" !in metadata.readText()) {
                "AGPL metadata crossed the public GPL-only IR boundary: ${metadata.name}"
            }
        }
    }
}

val verifyBeatorajaApi = tasks.register("verifyBeatorajaApi") {
    group = "verification"
    description = "Checks that an explicitly supplied beatoraja JAR exposes the pinned IR API."
    onlyIf { beatorajaJarProperty.isPresent }
    doLast {
        val runtimeJar = file(beatorajaJarProperty.get())
        check(runtimeJar.isFile) { "beatorajaJar is not a file: $runtimeJar" }
        ZipFile(runtimeJar).use { archive ->
            val required = listOf(
                "bms/player/beatoraja/ir/IRConnection.class",
                "bms/player/beatoraja/ir/IRChartData.class",
                "bms/player/beatoraja/ir/IRScoreData.class",
                "bms/player/beatoraja/ir/IRResponse.class",
            )
            required.forEach { entry ->
                check(archive.getEntry(entry) != null) { "pinned beatoraja API entry missing: $entry" }
            }
        }
    }
}

tasks.named("compileJava") {
    dependsOn(verifyBeatorajaApi)
}

val realBeatorajaCompatibility = tasks.register("realBeatorajaCompatibility") {
    group = "verification"
    description = "Compiles and packages main sources against an explicitly supplied real beatoraja JAR."
    dependsOn(tasks.named("compileJava"), tasks.named("jar"), verifyBeatorajaApi,
        verifyLicenseBoundary)
    doFirst {
        check(beatorajaJarProperty.isPresent) {
            "realBeatorajaCompatibility requires -PbeatorajaJar=/path/to/beatoraja.jar"
        }
    }
}

val checksums = tasks.register("checksums") {
    dependsOn(tasks.named("jar"), sourceJar, sourceBundle)
    outputs.dir(releaseDir)
    doLast {
        val destination = releaseDir.get().asFile
        destination.mkdirs()
        val artifacts = listOf(
            tasks.named<Jar>("jar").get().archiveFile.get().asFile,
            sourceJar.get().archiveFile.get().asFile,
            sourceBundle.get().archiveFile.get().asFile,
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
  "metadata": {"component": {"type": "application", "name": "oraja-training-ir", "version": "${project.version}", "licenses": [{"license": {"id": "GPL-3.0-only"}}]}},
  "components": [$dependency]
}
""".trimIndent() + "\n"
        )
    }
}

val releaseManifest = tasks.register("releaseManifest") {
    dependsOn(tasks.named("jar"), sourceJar, sourceBundle, sbom, licenseNotice)
    outputs.file(releaseDir.map { it.file("release-manifest.json") })
    doLast {
        fun sha256(input: File): String = MessageDigest.getInstance("SHA-256")
            .digest(input.readBytes())
            .joinToString("") { byte -> "%02x".format(byte) }
        val jar = tasks.named<Jar>("jar").get().archiveFile.get().asFile
        val sources = sourceJar.get().archiveFile.get().asFile
        val sourceArchive = sourceBundle.get().archiveFile.get().asFile
        val bom = releaseDir.get().file("sbom.cdx.json").asFile
        val license = releaseDir.get().file("LICENSE").asFile
        val notice = releaseDir.get().file("NOTICE").asFile
        val sourceRevision = providers.environmentVariable("GITHUB_SHA").orElse("local-unattested").get()
        val destination = releaseDir.get().file("release-manifest.json").asFile
        destination.writeText(
            """{
  "schema": "oraja.ir-release-manifest.v1",
  "component": "oraja-training-ir",
  "version": "${project.version}",
  "license": "GPL-3.0-only",
  "source_revision": "$sourceRevision",
  "beatoraja_api": "${beatorajaApiVersion.get()}",
  "real_beatoraja_jar_verified": ${beatorajaJarProperty.isPresent},
  "artifacts": [
    {"name": "${jar.name}", "sha256": "${sha256(jar)}"},
    {"name": "${sources.name}", "sha256": "${sha256(sources)}"},
    {"name": "${sourceArchive.name}", "sha256": "${sha256(sourceArchive)}"},
    {"name": "${bom.name}", "sha256": "${sha256(bom)}"},
    {"name": "${license.name}", "sha256": "${sha256(license)}"},
    {"name": "${notice.name}", "sha256": "${sha256(notice)}"}
  ]
}
""".trimIndent() + "\n"
        )
        releaseDir.get().file("sbom.cdx.json.sha256").asFile
            .writeText("${sha256(bom)}  ${bom.name}\n")
        releaseDir.get().file("release-manifest.json.sha256").asFile
            .writeText("${sha256(destination)}  ${destination.name}\n")
    }
}

val releaseArtifacts = tasks.register("releaseArtifacts") {
    dependsOn(tasks.named("jar"), sourceJar, sourceBundle, checksums, sbom, licenseNotice,
        releaseManifest, verifyLicenseBoundary, verifyBeatorajaApi)
    doLast {
        copy {
            from(tasks.named<Jar>("jar").get().archiveFile)
            from(sourceJar.get().archiveFile)
            from(sourceBundle.get().archiveFile)
            into(releaseDir)
        }
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
    dependsOn(contractTest, verifyLicenseBoundary, verifyBeatorajaApi)
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
                        name.set("GNU General Public License v3.0 only")
                        url.set("https://www.gnu.org/licenses/gpl-3.0.html")
                        distribution.set("repo")
                    }
                }
            }
        }
    }
}
