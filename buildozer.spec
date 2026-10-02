[app]
title = Imagenes a PDF
package.name = imagenesapdf
package.domain = com.openroute.imagenesapdf
source.dir = app
source.include_exts = py,png,jpg,kv,atlas
version = 0.1
requirements = python3,kivy==2.3.1,Pillow,img2pdf==0.4.4,numpy,opencv
android.archs = arm64-v8a
orientation = portrait
fullscreen = 0

[buildozer]
log_level = 2
warn_on_root = 0

# (str) Path to build artifact storage, absolute or relative to spec file
# build_dir = ./.buildozer

# (str) Path to build output (i.e. .apk, .aab, .ipa) storage
# bin_dir = ./bin

[app:permissions]
android.permissions = android.permission.READ_EXTERNAL_STORAGE, android.permission.WRITE_EXTERNAL_STORAGE

# (list) Android application meta-data to set (key=value format)
#android.meta_data =

# (list) Android library project to add (will be added in the
# project.properties automatically.)
#android.library_references =
#android.uses_library =

# (str) If you want to use service, uncomment and specify service names
#services = NAME:ENTRYPOINT_TO_PY,NAME2:ENTRYPOINT2_TO_PY

#
# OSX Specific
#

#
# Android specific
#

# (bool) Indicate if the application should be fullscreen or not
#fullscreen = 0

# (string) Presplash of the application
#presplash.filename = %(source.dir)s/data/presplash.png

# (string) Icon of the application
#icon.filename = %(source.dir)s/data/icon.png

# (str) Supported orientation (one of sensortab, sensor, portrait, landscape, portraitv, landscapev)
#orientation = portrait

# (list) List of service to declare
#services = NAME:ENTRYPOINT_TO_PY,NAME2:ENTRYPOINT2_TO_PY

# (bool) Indicate if the application should use the fullscreen flag
#fullscreen = 0

# (string) native splash screen background color
#android.splash_color = #FFFFFF

# (str) If automatic not want the default behaviour, uncomment and specify the API level
#android.api = 33

# (str) If automatic not want the default behaviour, uncomment and specify the NDK version
#android.ndk = 25b

# (str) If automatic not want the default behaviour, uncomment and specify the SDK version
#android.sdk = 33

# (str) ANT directory
#android.ant_path =

# (bool) If True, then skip trying to update the Android ant, Android SDK, Android API
# and all other dependencies
#android.skip_update = False

# (bool) If True, then skip trying to update the Android ant, Android SDK, Android API
# and all other dependencies
#p4a.skip_update = False

# (str) Bootstrap to use for android builds
# p4a.bootstrap = sdl2

# (int) port number to specify an explicit --port= p4a argument (eg for bootstrap flask)
#p4a.port =

# Control passing of --private-data-dir and --public-data-dir to p4a
# p4a.local_recipes =

# (str) If you want to use a specific branch of python-for-android, uncomment and specify the branch name
#p4a.branch = master

# (str) URL to use for the Android package build
#p4a.url =

# (str) Custom source folders to include in the APK
#p4a.source_dir =

# (list) python-for-android whitelist of recipes to use (comma separated)
#p4a.whitelist =

# (bool) If True, then skip trying to update the Android ant, Android SDK, Android API
#p4a.skip_update = False

# (str) Bootstrap to use for android builds
p4a.bootstrap = sdl2

# (int) port number to specify an explicit --port= p4a argument (eg for bootstrap flask)
#p4a.port =

# (str) Supported orientation (one of sensortab, sensor, portrait, landscape, portraitv, landscapev)
orientation = portrait

# (string) Presplash of the application
#presplash.filename = %(source.dir)s/data/presplash.png

# (string) Icon of the application
#icon.filename = %(source.dir)s/data/icon.png

# (str) If automatic not want the default behaviour, uncomment and specify the API level
android.api = 33

# (str) If automatic not want the default behaviour, uncomment and specify the NDK version
android.ndk = 25b

# (str) If automatic not want the default behaviour, uncomment and specify the SDK version
android.sdk = 33

# (bool) Indicate if the application should be fullscreen or not
fullscreen = 0

# (str) Python-for-Android log output: stdout/stderr to logcat
p4a.log_output_from_stdlib = 1

# (bool) Show Android logcat when running from buildozer
logcat = 1
