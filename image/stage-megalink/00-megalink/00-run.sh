#!/bin/bash -e

# build.sh leaves the installer's source tree here: deploy/ and dist/ with the
# wheel in it. The installer is the same one used on a running Pi, told with
# --image that it is preparing an image rather than a machine. In /var/tmp, not
# /tmp: on_chroot mounts a fresh tmpfs over the image's /tmp, which would hide
# anything copied there beforehand.
rm -rf "${ROOTFS_DIR}/var/tmp/megalink-src"
cp -a files/megalink "${ROOTFS_DIR}/var/tmp/megalink-src"

on_chroot <<CHROOT
sh /var/tmp/megalink-src/deploy/install.sh --image --mode gui
rm -rf /var/tmp/megalink-src

# stage1 leaves root with the password "root". Nobody needs to log in to a
# display at all, so root and the first user are both locked; a user added
# through Imager is the way in for anyone who wants one.
passwd -l root
passwd -l "${FIRST_USER_NAME}"
CHROOT
