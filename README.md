# puppet2hiera

## Overview

Converts Puppet/OpenVox resource declarations and class declarations into Hiera compatible YAML format, preserving quoting style from the original Puppet code.

## Usage

```shell
# read from file
python3 puppet2hiera.py <input_file> [output_file]

# read from stdin
python3 puppet2hiera.py -

# read from pipe
cat manifest.pp | python3 puppet2hiera.py -
```

## Example

A simple example that demonstrates how this script works:

```shell
cat << EOF > /tmp/puppet_code
class { 'test_module':
  ensure => 'present',
  manage_stuff => true,
  config => {
    param1 => 'value1',
    param2 => 'value2',
  },
  users => [
    'john',
    'marge',
  ],
}
EOF

python3 puppet2hiera.py /tmp/puppet_code
test_module::ensure: 'present'
test_module::manage_stuff: true
test_module::config:
  param1: 'value1'
  param2: 'value2'
test_module::users:
  - 'john'
  - 'marge'
```

And a real-world example that demonstrates one of the main use-cases:

```shell
puppet resource user > /tmp/puppet_users

python3 puppet2hiera.py /tmp/puppet_users
```
