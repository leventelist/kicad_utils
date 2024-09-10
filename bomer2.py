#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import sys

sys.path.append('/usr/local/share/kicad/plugins/')

import kicad_netlist_reader
import csv
import sqlite3
import argparse
import os


def get_supplier_name_by_id(supplier_id, database_cursor):

	supplier_t=(supplier_id,)
	database_cursor.execute('SELECT * FROM supplier WHERE id=?', supplier_t)
	supplier_row=database_cursor.fetchone()
	if supplier_row == None:
		print("No such supplier '" + str(supplier) + "'")
		return None
	else:
		return supplier_row['name']


#def eprint(*args, **kwargs):
#    print(*args, file=sys.stderr, **kwargs)

parser = argparse.ArgumentParser(description='This script creates bom from intermediate netlist (xml) and the database.')
parser.add_argument('sch_file')
parser.add_argument('-d', '--db_file', help='filename of the database', action='store', required=True)
parser.add_argument('-o', '--out_file', help='filename of the output csv', action='store', required=True)
parser.add_argument('-c', '--compact', help='generate compact list', action='store_true')
parser.add_argument('-j', '--jlc', help='generate BOM list for JLC', action='store_true')
args = parser.parse_args()

#Generate bom.xml
xml_gen = "kicad-cli sch export python-bom -o bom.xml " + args.sch_file
os.system(xml_gen)

net = kicad_netlist_reader.netlist('bom.xml')

#Open database
print("\nOpening database " + args.db_file + "\n")

try:
    f = open(args.out_file, 'w')
except IOError:
    e = "Can't open output file for writing: " + args.out_file
    print(__file__, ":", e, sys.stderr)
    f = sys.stdout

db_conn = sqlite3.connect(args.db_file)
db_conn.row_factory = sqlite3.Row
db_cursor = db_conn.cursor()

# Create a new csv writer object to use as the output formatter
out = csv.writer(f, lineterminator='\n', delimiter=',', quotechar='\"', quoting=csv.QUOTE_ALL)

# Output a set of rows for a header providing general information
if args.compact == True:
	out.writerow(['Refs', 'Qty', 'Value', 'Value2', 'Footprint'])
elif args.jlc == True:
	out.writerow(['Comment', 'Designator', 'Footprint', 'LCSC Part #'])
else:
	out.writerow(['Source:', net.getSource()])
	out.writerow(['Date:', net.getDate()])
	out.writerow(['Tool:', net.getTool()])
	out.writerow(['Generator:', sys.argv[0]])
	out.writerow(['Component Count:', len(net.components)])
	out.writerow(['Refs', 'id', 'Qty', 'Value', 'Value2', 'Value3', 'Device description', 'Cmp name', 'Footprint', 'CAD description', 'Ordering code', 'Supplier', 'Subtotal', 'Total'])


grouped = net.groupComponents()

total = 0
incomplete = False

for group in grouped:
	refs = ""
	first_component = True

	# Add the reference of every component in the group and keep a reference
	# to the component so that the other data can be filled in once per group
	for component in group:
		if first_component:
			refs = component.getRef()
			first_component = False
		else:
			refs += "," + component.getRef()

	c = component
	comp_id = c.getField("id")
	variant = c.getField("variant")

	dev_price = 0
	subtotal = 0
	price = 0
	supplier = 0
	ord_code = ''
	supplier_name = ''
	ppu = 1
	val2 = ''
	val3 = ''
	short_footprint = ''
	description = ''

	if variant == '':
		variant = 1

	if comp_id != '0' and comp_id != '' and comp_id != 0:
		query_t = (comp_id, variant)
		#Get all rows, and select our favorite source
		db_cursor.execute('SELECT * FROM source WHERE dev_id={0[0]}'.format(query_t))
		while True:
			source_row = db_cursor.fetchone()
			if source_row == None:
				break
			if args.jlc == True:
				if source_row['sup_id'] == 13: #This is hard coded. Should have some other mechanism.
					break
				else:
					source_row = None
			else:
				break
		if source_row == None:
			print ('No source data found for \'' + comp_id + '\'.')
			incomplete = True
		else:
			supplier = source_row['sup_id']
			ord_code = source_row['ordering_code']
			price = source_row['uprice']
			ppu = source_row['ppu']
			dev_price = price / ppu
			subtotal = dev_price * len(group)
			total += subtotal
			supplier_name = get_supplier_name_by_id(supplier, db_cursor)
			if supplier_name == None:
				print("Couldn't find supplier for id \'" + comp_id + '\'.')
		db_cursor.execute('SELECT * FROM device WHERE id={0[0]}'.format(query_t))
		dev_row = db_cursor.fetchone()
		if type(dev_row) != type(None):
			if type(dev_row['value2']) != type(None):
				val2 = dev_row['value2']
			else:
				val2 = ''
			if type(dev_row['value3']) != type(None):
				val3 = dev_row['value3']
			else:
				val3 = ''
		else:
			val2 = ''
			val3 = ''

		db_cursor.execute('SELECT * FROM cad_data WHERE cad_tool=2 AND variant={0[1]} AND dev_id={0[0]}'.format(query_t))
		cad_data_row = db_cursor.fetchone()

		if cad_data_row == None:
			incomplete = True
			print("No CAD data for " + component.getRef() + ".")
		else:
			short_footprint = cad_data_row['short_footprint']

		if type(dev_row) != type(None) and type(dev_row['description']) != type(None):
			description = dev_row['description']
		else:
			description = ''

		if args.compact == True:
			out.writerow([refs, len(group), c.getValue(), val2, short_footprint])
		elif args.jlc == True:
			out.writerow([c.getValue(), refs, short_footprint, ord_code])
		else:
		# Fill in the component groups common data
			out.writerow([refs, comp_id, len(group), c.getValue(), val2, val3, description, c.getPartName(), c.getFootprint(),
			c.getDescription(), ord_code, supplier_name, subtotal, ""])

	else:
		if comp_id != '0':
			print("\nNo ID found for component(s):\n\n\t" + refs)
			incomplete = True

if args.compact == True:
	#Do nothing.
	print("Don't printing footer")
elif args.jlc == True:
	#Do nothing.
	print("Don't printing footer")
else:
	out.writerow(['', '', '', '', '', '', '', '', '', '', '', '', '', total])

if incomplete:
	print("\n\nYour output might be incomplete.")

#clean up
db_cursor.close()
f.close
