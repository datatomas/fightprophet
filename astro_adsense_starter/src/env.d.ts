type CountryMasterIndex = import('./lib/country-master').CountryMasterIndex;

declare namespace App {
  interface Locals {
    countryMaster: CountryMasterIndex;
  }
}
