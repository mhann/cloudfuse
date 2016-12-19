#
# @file  cloud-fuse.py
#
# @brief Main entrypoint into the cloud-fuse software.
#

from __future__ import print_function, absolute_import, division

import hashlib
import logging
import math
import md5
import os
import importlib

import helpers.blocks
import helpers.filesystem
import helpers.database

from errno import ENOENT
from stat import S_IFDIR, S_IFREG
from sys import argv, exit
from time import time

from sqlalchemy import Column, String, Integer, ForeignKey, create_engine, Boolean, Date
from sqlalchemy.orm import relationship, sessionmaker
from sqlalchemy.ext.declarative import declarative_base

from fuse import FUSE, FuseOSError, Operations, LoggingMixIn, fuse_get_context

# Base from sqlalchemy orm so that we can derive classes from it.
Base = declarative_base()


class Node(Base):
    __tablename__ = 'node'
    id = Column(Integer, primary_key=True)
    parent_id = Column(Integer, ForeignKey('node.id'))
    children = relationship("Node")
    name = Column(String)
    size = Column(Integer)
    permissions = Column(Integer)
    directory = Column(Boolean)
    create_time = Column(Date)
    update_time = Column(Date)
    read_time = Column(Date)
    parent = relationship("Node", remote_side=[id])
    blocks = relationship("Block")

    @staticmethod
    def get_top_level_nodes():
        return session.query(Node).order_by(Node.id).filter(Node.parent is None).all()

    @staticmethod
    def get_children_of_node(parent):
        child_nodes = []

        for row in session.query(Node).order_by(Node.id).filter(Node.parent == parent).all():
            print(row.name)
            child_nodes.append(row)

        return child_nodes

    @staticmethod
    def get_node_from_abs_path(path):
        split_path = path.split("/")

        last_parent_node = None

        for path_section in split_path:
            print("Working on path segment: {}".format(path_section))
            if path_section == "":
                continue

            try:
                print("Looking for node with parentid {} and name {}".format(last_parent_node, path_section))
                last_parent_node = session.query(Node).order_by(Node.id).filter(Node.parent == last_parent_node,
                                                                                Node.name == path_section).one()
                print("Found match for: {}, which was: {}".format(path_section, Node.id))
            except:
                # No file existed in this path
                return False

        return last_parent_node

    def get_size(self):
        print("Getting size for node with name: {}".format(self.name))

        total_size = 0

        for file_block in self.blocks:
            print("Found block with size: {}".format(file_block.size))
            total_size += file_block.size

        return total_size


class Block(Base):
    __tablename__ = 'block'
    id = Column(Integer, primary_key=True)
    hash = Column(String)
    size = Column(Integer)
    node = Column(Integer, ForeignKey('node.id'))
    position = Column(Integer)


# Main class passed to fuse - this is where we define the functions that are called by fuse.
class Context(LoggingMixIn, Operations):
    def removexattr(self, att1, att2):
        return 0

    def rename(self, old, new):
        # Do something here
        return os.EEXIST

    def getattr(self, path, fh=None):
        node_for_path = Node.get_node_from_abs_path(path)
        if node_for_path:
            if (node_for_path.directory):
                attr = dict(st_mode=(S_IFDIR | 0o755), st_nlink=2 + len(node_for_path.children), st_size=0)
            else:
                attr = dict(st_mode=(S_IFREG | 0o755), st_nlink=1,
                            st_size=node_for_path.get_size())
        elif path == '/':
            attr = dict(st_mode=(S_IFDIR | 0o755), st_nlink=2 + len(Node.get_top_level_nodes()))
        else:
            raise FuseOSError(ENOENT)

        attr['st_ctime'] = attr['st_mtime'] = time()
        return attr

    def truncate(self, path, length, fh=None):
        block_path = helpers.blocks.get_block_root(path)

        print("Deleting all files in: {}".format(block_path))

        for block in Node.get_node_from_abs_path(path).blocks:
            filesystem.delete_file(block_path + block.position)

    def read(self, path, size, offset, fh):

        node_to_read = Node.get_node_from_abs_path(path)

        if not node_to_read:
            raise RuntimeError('Could not find node for path: %r' % path)

        offset_from_first_block = offset % 512
        first_block = int(math.ceil(offset / 512))
        number_of_blocks = int(math.ceil((offset_from_first_block + size) / 512))

        if number_of_blocks > len(node_to_read.blocks):
            number_of_blocks = len(node_to_read.blocks)

        print("Number of blocks: {}".format(number_of_blocks))

        if offset == 0:
            first_block = 1

        file_content = ""

        for current_block_index in range(first_block, first_block + number_of_blocks):
            if (current_block_index == first_block):
                bytes_to_read = 512 - offset_from_first_block
                offset_for_block = offset_from_first_block
            elif (current_block_index == first_block + number_of_blocks):
                bytes_to_read = 512 - (512 - offset_from_first_block)
                offset_for_block = 0
            else:
                bytes_to_read = 512
                offset_for_block = 0

            print("Would read {} bytes from block #{} at offset {}".format(bytes_to_read, current_block_index,
                                                                           offset_for_block))

            block_path = helpers.blocks.get_block_root(path)

            print("Reading {} bytes from {} at offset {}".format(bytes_to_read, block_path + str(current_block_index),
                                                                 offset_for_block))
            print("Reading whole block")
            whole_block_contents = filesystem.readFile(helpers.blocks.get_block_root(path) + str(current_block_index))
            block_contents_from_offset = whole_block_contents[offset_for_block:(offset_for_block + bytes_to_read)]

            print("Would return: {}".format(block_contents_from_offset))

            file_content += block_contents_from_offset

        return file_content

    def readdir(self, path, fh):
        return ['.', '..'] + [node_in_path.name for node_in_path in Node.get_children_of_node(Node.get_node_from_abs_path(path))]

    def mkdir(self, path, mode):
        if not Node.get_node_from_abs_path(path):
            if len(path.split('/')[:-1]) == 1:
                print("Adding to root")
                parent = Node(name=path.split('/')[1], directory=True)
                session.add(parent)
                session.commit()
                return 0

            path_root = path.split('/')[:-1]
            path_root = '/'.join(path_root)

            parent_node = Node.get_node_from_abs_path(path_root)

            if not parent_node.directory:
                print("Trying to add node to non-directory node!")
                # I doubt EEXIST is the correct thing to be returning here.
                return os.EEXIST

            new_file = Node(name=path.split('/')[1], directory=True)
            parent_node.children.append(new_file)
            session.commit()

            block_path = helpers.blocks.get_block_root(path)

            filesystem.make_directory(block_path)

            return new_file.id

        return os.EEXIST

    def create(self, path, mode):
        print("Create called")

        if not Node.get_node_from_abs_path(path):
            if len(path.split('/')[:-1]) == 1:
                # This is a path without multiple slashes such as:
                #    /home.txt
                # This would NOT include:
                #    /home/home.txt
                print("Adding to root")
                new_file = Node(name=path.split('/')[1])
                session.add(new_file)
                session.commit()
            else:
                path_root = path.split('/')[:-1]
                path_root = '/'.join(path_root)

                parent_node = Node.get_node_from_abs_path(path_root)

                if not parent_node.directory:
                    print("Trying to add node to non-directory node!")
                    # I doubt EEXIST is the correct thing to be returning here.
                    return os.EEXIST

                new_file = Node(name=path.split('/')[-1], directory=False)
                parent_node.children.append(new_file)
                session.commit()

            block_path = helpers.blocks.get_block_root(path)

            print("Block path is: {}".format(block_path))

            filesystem.make_directory(block_path)

            return new_file.id

        return os.EEXIST

    def open(self, path, flags):
        # NOT a real fd - but will do for simple testing
        return Node.get_node_from_abs_path(path).id

    def write(self, path, data, offset, fh):
        to_write_node = Node.get_node_from_abs_path(path)
        block_path = helpers.blocks.get_block_root(path)

        block_size = 512
        first_block_for_to_write_data = int(math.ceil(offset / block_size))
        first_block_offset = int(offset % block_size)
        number_of_blocks = int(math.ceil((first_block_offset + block_size) / block_size))

        if offset == 0:
            first_block_for_to_write_data = 1

        current_block = first_block_for_to_write_data

        test = helpers.blocks.string_to_chunks(data, block_size)

        print(list(test))

        for position, data_block in enumerate(helpers.blocks.string_to_chunks(data, block_size)):
            if (position == 0):
                # This is the first block that we are writing to
                bytes_to_write = block_size - first_block_offset
                offset_for_block = first_block_offset
            elif (position == number_of_blocks):
                # This is the last block that we are writing to
                bytes_to_write = block_size - (block_size - first_block_offset)
                offset_for_block = 0
            else:
                bytes_to_write = block_size
                offset_for_block = 0

            data_hash = hashlib.md5()
            data_hash.update(data_block)

            current_block = first_block_for_to_write_data + position

            block_instance = Block()
            block_instance.size = len(data_block)
            block_instance.hash = data_hash.hexdigest()
            block_instance.position = position

            session.add(block_instance)
            to_write_node.blocks.append(block_instance)
            session.commit()

            print("Writing data {} of size {} to block {} at offset {}".format(data_block, len(data_block), current_block,
                                                                               offset_for_block))

            current_block_contents = filesystem.readFile(block_path + str(current_block))
            if not current_block_contents:
                current_block_contents = ""

            new_block_contents = current_block_contents[:offset_for_block] + data_block
            filesystem.write_file(block_path + str(current_block), new_block_contents)

        return len(data)


if __name__ == '__main__':
    if len(argv) != 2:
        print('usage: %s <mountpoint>' % argv[0])
        exit(1)

    logging.basicConfig(level=logging.DEBUG)

    engine = create_engine('sqlite:///')
    sessionMaker = sessionmaker()
    sessionMaker.configure(bind=engine)
    Base.metadata.create_all(engine)
    session = sessionMaker()

    parent1 = Node(name='test', directory=True)
    parent1.children.append(Node(name='test2', directory=True))

    session.add(parent1)
    session.commit()

    print("Listing all nodes")
    for node in session.query(Node):
        print("Node: {}".format(node.name))

    Node.get_node_from_abs_path('/test/test2').children.append(Node(name='test21'))

    session.commit()

    print(Node.get_node_from_abs_path('/test/test2/test21').name)

    print("Testing drivers")
    driverImport = importlib.import_module("drivers.filesystem", __name__)

    global filesystem
    filesystem = driverImport.drivers.filesystem.FileSystem()

    fuse = FUSE(Context(), argv[1], ro=False, foreground=True, nothreads=True)
