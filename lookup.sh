#!/bin/sh


while read doi; do
  echo "lookup doi:$doi"
  curl --location --silent --header "Accept: application/x-bibtex" "https://doi.org/$doi" >> newrefs.bib
done

