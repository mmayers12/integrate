import re
import os
import tarfile
import shutil
import subprocess
import concurrent.futures
import collections
import textwrap

import urllib.request
import py2neo

import hetio.readwrite
import hetio.neo4j


def replace_text(path, find, repl):
    """
    Read a text file, replace the text specified by find with repl,
    and overwrite the file with the modified version.
    """
    with open(path) as read_file:
        text = read_file.read()
    pattern = re.escape(find)
    text = re.sub(pattern, repl, text)
    with open(path, 'wt') as write_file:
        write_file.write(text)

def append_text(path, text):
    """Append text to a file, preceeded by a newline"""
    with open(path, 'at') as write_file:
        write_file.write('\n')
        write_file.write(text)

def create_instance(version, db_id, port=7474, overwrite=False):
    """Create neo4j instance"""

    # Download neo4j
    filename = '{}-unix.tar.gz'.format(version)
    path = os.path.join('neo4j', filename)
    if not os.path.exists(path):
        url = 'http://neo4j.com/artifact.php?name={}'.format(filename)
        urllib.request.urlretrieve(url, path)

    # Extract to file
    tar_file = tarfile.open(path, 'r:gz')
    tar_file.extractall('neo4j')
    directory = os.path.join('neo4j', '{}_{}'.format(version, db_id))
    if os.path.isdir(directory) and overwrite:
        shutil.rmtree(directory)
    os.rename(os.path.join('neo4j', version), directory)

    # Modify neo4j-server.properties
    path = os.path.join(directory, 'conf', 'neo4j-server.properties')
    # disable auth to access Neo4j
    replace_text(path, 'dbms.security.auth_enabled=true', 'dbms.security.auth_enabled=false')
    replace_text(path,
        'org.neo4j.server.webserver.port=7474',
        'org.neo4j.server.webserver.port={}'.format(port))
    replace_text(path,
        'org.neo4j.server.webserver.https.enabled=true',
        'org.neo4j.server.webserver.https.enabled=false')

    # Modify neo4j.properties
    path = os.path.join(directory, 'conf', 'neo4j.properties')
    # keep only the most recent non-empty log
    # http://neo4j.com/docs/stable/configuration-logical-logs.html
    replace_text(path, '#keep_logical_logs=7 days', 'keep_logical_logs=false')

    append_text(path, '\n')
    text = textwrap.dedent('''\
    # Decrease checkpointing.
    # See https://github.com/neo4j/neo4j/issues/6787#issuecomment-202808178
    dbms.checkpoint.interval.time=180m
    dbms.checkpoint.interval.tx=10000000
    ''')
    append_text(path, text)

    return directory

def hetnet_to_neo4j(path, neo4j_dir, port, database_path='data/graph.db'):
    """
    Read a hetnet from file and import it into a new neo4j instance.
    """
    neo4j_bin = os.path.join(neo4j_dir, 'bin', 'neo4j')
    subprocess.run([neo4j_bin, 'start'])
    error = None
    try:
        graph = hetio.readwrite.read_graph(path)
        uri = 'http://localhost:{}/db/data/'.format(port)
        hetio.neo4j.export_neo4j(graph, uri, 1000, 250, show_progress=True)
    except Exception as e:
        error = e
        print(neo4j_dir, e)
    finally:
        subprocess.run([neo4j_bin, 'stop'])
    if not error:
        database_dir = os.path.join(neo4j_dir, database_path)
        remove_logs(database_dir)

def remove_logs(database_dir):
    """Should only run when server is shutdown."""
    filenames = os.listdir(database_dir)
    removed = list()
    for filename in filenames:
        if (filename.startswith('neostore.transaction.db') or
            filename.startswith('messages.log')):
            path = os.path.join(database_dir, filename)
            os.remove(path)
            removed.append(filename)
    return removed

if __name__ == "__main__":

    # read cross validation index
    with open("../crossval_idx.txt", "r") as fin:
        crossval_idx = int(fin.read().strip())

    print("Cross validation index is {}".format(crossval_idx))

    # Options
    neo4j_version = 'neo4j-community-2.3.3'
    db_name = 'rephetio-v2.0'

    port_0 = 7500 + 10*crossval_idx

    # Identify permuted network files
    permuted_filenames = sorted(x for x in os.listdir('data/permuted') if 'hetnet_perm' in x)
    print('Permuted filenames:', permuted_filenames)

    # Initiate Pool
    pool = concurrent.futures.ProcessPoolExecutor(max_workers = 2)
    port_to_future = collections.OrderedDict()

    # Export unpermuted network to neo4j
    neo4j_dir = create_instance(neo4j_version, db_name, port_0, overwrite=True)
    future = pool.submit(hetnet_to_neo4j, path='data/hetnet.json.bz2', neo4j_dir=neo4j_dir, port=port_0)
    port_to_future[port_0] = future

    # Export permuted network to neo4j
    for i, filename in enumerate(permuted_filenames):
        i += 1
        port = port_0 + i
        db_id = '{}_perm-{}'.format(db_name, i)
        neo4j_dir = create_instance(neo4j_version, db_id, port, overwrite=True)
        path = os.path.join('data', 'permuted', filename)
        future = pool.submit(hetnet_to_neo4j, path=path, neo4j_dir=neo4j_dir, port = port)
        port_to_future[port] = future

    # Shutdown pool
    pool.shutdown()
    print('Complete')

    # Print Exceptions
    for port, future in port_to_future.items():
        exception = future.exception()
        if exception is None:
            continue
        print('\nERROR: Exception importing on port {}:'.format(port))
        print(exception)
