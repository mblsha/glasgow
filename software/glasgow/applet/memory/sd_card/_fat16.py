from collections.abc import Buffer, Mapping
import os
import struct


__all__ = ["FAT16Error", "FAT16Image", "create_fat16_image"]


SECTOR_SIZE = 512
DEFAULT_IMAGE_SIZE = 16 * 1024 * 1024
PARTITION_LBA = 32
SECTORS_PER_CLUSTER = 2
RESERVED_SECTORS = 1
FAT_COUNT = 2
SECTORS_PER_FAT = 64
ROOT_ENTRIES = 512


class FAT16Error(ValueError):
    pass


def _short_name(name: str) -> bytes:
    name = name.upper()
    if "/" in name or "\\" in name or name in ("", ".", ".."):
        raise FAT16Error(f"invalid FAT 8.3 name {name!r}")
    base, dot, extension = name.partition(".")
    if "." in extension or len(base) not in range(1, 9) or len(extension) > 3:
        raise FAT16Error(f"name is not representable as FAT 8.3: {name!r}")
    try:
        encoded = (base.ljust(8) + extension.ljust(3)).encode("ascii")
    except UnicodeEncodeError as error:
        raise FAT16Error(f"name is not ASCII: {name!r}") from error
    if any(character in '"*+,/:;<=>?[\\]|' for character in name):
        raise FAT16Error(f"invalid FAT 8.3 name {name!r}")
    return encoded


def create_fat16_image(path, files: Mapping[str, Buffer] = (), *,
        size: int = DEFAULT_IMAGE_SIZE, volume_label="GLASGOW SD"):
    """Create a deterministic partitioned FAT16 image suitable for old SD hosts."""

    if size % SECTOR_SIZE or size < 4 * 1024 * 1024 or size > DEFAULT_IMAGE_SIZE:
        raise FAT16Error("FAT16 image size must be sector aligned and between 4 and 16 MiB")
    total_sectors = size // SECTOR_SIZE
    partition_sectors = total_sectors - PARTITION_LBA
    root_sectors = (ROOT_ENTRIES * 32 + SECTOR_SIZE - 1) // SECTOR_SIZE
    data_start = RESERVED_SECTORS + FAT_COUNT * SECTORS_PER_FAT + root_sectors
    cluster_count = (partition_sectors - data_start) // SECTORS_PER_CLUSTER
    if cluster_count < 4085 or cluster_count + 2 > SECTORS_PER_FAT * SECTOR_SIZE // 2:
        raise FAT16Error("geometry does not produce a valid FAT16 volume")

    normalized_files = []
    seen_names = set()
    for name, contents in files.items() if isinstance(files, Mapping) else files:
        short_name = _short_name(name)
        if short_name in seen_names:
            raise FAT16Error(f"duplicate FAT name {name!r}")
        seen_names.add(short_name)
        normalized_files.append((short_name, bytes(contents)))
    if len(normalized_files) > ROOT_ENTRIES:
        raise FAT16Error("too many root-directory entries")

    image = bytearray(size)
    image[510:512] = b"\x55\xaa"
    partition = bytearray(16)
    partition[0] = 0x00
    partition[1:4] = b"\x01\x01\x00"
    partition[4] = 0x06
    partition[5:8] = b"\xfe\xff\xff"
    struct.pack_into("<II", partition, 8, PARTITION_LBA, partition_sectors)
    image[446:462] = partition

    volume_offset = PARTITION_LBA * SECTOR_SIZE
    boot = memoryview(image)[volume_offset:volume_offset + SECTOR_SIZE]
    boot[0:3] = b"\xeb\x3c\x90"
    boot[3:11] = b"GLASGOW "
    struct.pack_into("<HBHBHHBHHHII", boot, 11,
        SECTOR_SIZE, SECTORS_PER_CLUSTER, RESERVED_SECTORS, FAT_COUNT,
        ROOT_ENTRIES, partition_sectors, 0xf8, SECTORS_PER_FAT,
        32, 64, PARTITION_LBA, 0)
    boot[36] = 0x80
    boot[38] = 0x29
    struct.pack_into("<I", boot, 39, 0x474c4153)
    boot[43:54] = volume_label.upper()[:11].ljust(11).encode("ascii")
    boot[54:62] = b"FAT16   "
    boot[510:512] = b"\x55\xaa"

    fat = bytearray(SECTORS_PER_FAT * SECTOR_SIZE)
    struct.pack_into("<HH", fat, 0, 0xfff8, 0xffff)
    cluster_bytes = SECTORS_PER_CLUSTER * SECTOR_SIZE
    next_cluster = 2
    allocations = []
    for short_name, contents in normalized_files:
        cluster_length = (len(contents) + cluster_bytes - 1) // cluster_bytes
        first_cluster = next_cluster if cluster_length else 0
        if next_cluster + cluster_length > cluster_count + 2:
            raise FAT16Error("files do not fit in FAT16 image")
        for index in range(cluster_length):
            cluster = next_cluster + index
            following = 0xffff if index == cluster_length - 1 else cluster + 1
            struct.pack_into("<H", fat, cluster * 2, following)
        allocations.append((short_name, contents, first_cluster, cluster_length))
        next_cluster += cluster_length

    fat_offset = volume_offset + RESERVED_SECTORS * SECTOR_SIZE
    for index in range(FAT_COUNT):
        start = fat_offset + index * len(fat)
        image[start:start + len(fat)] = fat

    root_offset = fat_offset + FAT_COUNT * len(fat)
    data_offset = volume_offset + data_start * SECTOR_SIZE
    # 2026-07-01 00:00:00, fixed so generated images are byte-for-byte reproducible.
    dos_date = ((2026 - 1980) << 9) | (7 << 5) | 1
    for entry_index, (short_name, contents, first_cluster, cluster_length) in \
            enumerate(allocations):
        entry_offset = root_offset + entry_index * 32
        image[entry_offset:entry_offset + 11] = short_name
        image[entry_offset + 11] = 0x20
        struct.pack_into("<HHHHHHH", image, entry_offset + 14,
            0, dos_date, dos_date, 0, 0, dos_date, first_cluster)
        struct.pack_into("<I", image, entry_offset + 28, len(contents))
        if cluster_length:
            start = data_offset + (first_cluster - 2) * cluster_bytes
            image[start:start + len(contents)] = contents

    with open(os.fspath(path), "wb") as output:
        output.write(image)


class FAT16Image:
    """Minimal structural reader used to verify old-host FAT16 test images."""

    def __init__(self, image: Buffer):
        self.image = bytes(image)
        if len(self.image) < SECTOR_SIZE or self.image[510:512] != b"\x55\xaa":
            raise FAT16Error("invalid MBR signature")
        partition_type = self.image[446 + 4]
        if partition_type not in (0x04, 0x06, 0x0e):
            raise FAT16Error(f"first partition is not FAT16 (type {partition_type:#04x})")
        self.partition_lba, self.partition_sectors = struct.unpack_from(
            "<II", self.image, 446 + 8)
        self.volume_offset = self.partition_lba * SECTOR_SIZE
        if self.volume_offset + SECTOR_SIZE > len(self.image):
            raise FAT16Error("FAT16 boot sector is outside image")
        boot = self.image[self.volume_offset:self.volume_offset + SECTOR_SIZE]
        if boot[510:512] != b"\x55\xaa":
            raise FAT16Error("invalid FAT16 boot-sector signature")
        self.bytes_per_sector, = struct.unpack_from("<H", boot, 11)
        self.sectors_per_cluster = boot[13]
        self.reserved_sectors, = struct.unpack_from("<H", boot, 14)
        self.fat_count = boot[16]
        self.root_entries, = struct.unpack_from("<H", boot, 17)
        self.sectors_per_fat, = struct.unpack_from("<H", boot, 22)
        if self.bytes_per_sector != SECTOR_SIZE or self.sectors_per_cluster == 0 or \
                self.fat_count == 0 or self.sectors_per_fat == 0:
            raise FAT16Error("unsupported FAT16 geometry")
        root_sectors = (self.root_entries * 32 + SECTOR_SIZE - 1) // SECTOR_SIZE
        self.fat_offset = self.volume_offset + self.reserved_sectors * SECTOR_SIZE
        self.root_offset = self.fat_offset + \
            self.fat_count * self.sectors_per_fat * SECTOR_SIZE
        self.data_offset = self.root_offset + root_sectors * SECTOR_SIZE
        if self.data_offset > len(self.image):
            raise FAT16Error("FAT16 metadata extends past image")

        self.files = {}
        for index in range(self.root_entries):
            entry = self.image[self.root_offset + index * 32:self.root_offset + (index + 1) * 32]
            if not entry or entry[0] == 0x00:
                break
            if entry[0] == 0xe5 or entry[11] & 0x18:
                continue
            base = entry[:8].decode("ascii").rstrip()
            extension = entry[8:11].decode("ascii").rstrip()
            name = base + ("." + extension if extension else "")
            first_cluster, = struct.unpack_from("<H", entry, 26)
            length, = struct.unpack_from("<I", entry, 28)
            self.files[name] = (first_cluster, length)

    @classmethod
    def from_path(cls, path):
        with open(os.fspath(path), "rb") as input_file:
            return cls(input_file.read())

    def read_file(self, name: str) -> bytes:
        short_name = _short_name(name)
        display_name = short_name[:8].decode().rstrip()
        extension = short_name[8:].decode().rstrip()
        display_name += "." + extension if extension else ""
        try:
            cluster, length = self.files[display_name]
        except KeyError as error:
            raise FAT16Error(f"file not found: {display_name}") from error
        if length == 0:
            return b""

        result = bytearray()
        visited = set()
        cluster_bytes = self.sectors_per_cluster * SECTOR_SIZE
        while cluster < 0xfff8:
            if cluster < 2 or cluster in visited:
                raise FAT16Error("invalid or cyclic FAT cluster chain")
            visited.add(cluster)
            offset = self.data_offset + (cluster - 2) * cluster_bytes
            if offset + cluster_bytes > len(self.image):
                raise FAT16Error("FAT cluster is outside image")
            result.extend(self.image[offset:offset + cluster_bytes])
            cluster, = struct.unpack_from("<H", self.image, self.fat_offset + cluster * 2)
        if len(result) < length:
            raise FAT16Error("FAT cluster chain is shorter than directory size")
        return bytes(result[:length])
